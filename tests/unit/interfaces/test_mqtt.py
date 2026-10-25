"""Unit test: MQTTEventSink/MQTTEventSource against a stand-in `aiomqtt` module (no broker)."""

import asyncio
import gc
import json
import sys
import types
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from craftainer_crud.interfaces.base import MQTTEventSink, MQTTEventSource, _background_tasks
from craftainer_crud.interfaces.subscriber_ids import mint_subscriber_id

# A stand-in for the broker credential the sink forwards; not a real secret.
_FAKE_BROKER_CREDENTIAL = "p"


class _Message:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()


class _FakeClient:
    """Records how it was constructed and used; replays two canned messages."""

    instances: list[_FakeClient] = []  # noqa: RUF012 -- test-only registry
    fail_subscribe = False

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 -- mirrors aiomqtt.Client(**kwargs)
        self.kwargs = kwargs
        self.published: list[tuple[str, str, int]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.closed = False
        self.block = False
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))

    async def subscribe(self, topic: str, qos: int) -> None:
        if _FakeClient.fail_subscribe:
            msg = "refused"
            raise OSError(msg)
        self.subscribed.append((topic, qos))

    @property
    def messages(self) -> AsyncIterator[_Message]:
        async def _replay() -> AsyncIterator[_Message]:
            if self.block:
                await asyncio.Event().wait()
            yield _Message({"action": "create", "record_id": 1})
            yield _Message({"action": "delete", "record_id": 1})

        return _replay()


@pytest.fixture(autouse=True)
def _fake_aiomqtt(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances.clear()
    module = types.ModuleType("aiomqtt")
    module.Client = _FakeClient  # type: ignore[attr-defined]
    module.ProtocolVersion = types.SimpleNamespace(V5="v5")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiomqtt", module)


async def test_sink_publishes_an_enveloped_event_at_qos_1() -> None:
    """publish() opens a client, sends one QoS-1 message to the resource's topic, and closes."""
    sink = MQTTEventSink(
        hostname="broker",
        port=1884,
        resource="hero",
        keepalive=30,
        username="u",
        password=_FAKE_BROKER_CREDENTIAL,
    )
    await sink.publish(resource="hero", record_id=7, action="create", snapshot={"name": "x"})
    (client,) = _FakeClient.instances
    assert client.kwargs["hostname"] == "broker"
    assert client.kwargs["port"] == 1884
    assert client.kwargs["username"] == "u"
    topic, payload, qos = client.published[0]
    assert (topic, qos) == ("crud-events/hero", 1)
    assert json.loads(payload)["record_id"] == 7
    assert client.closed


async def test_source_resumes_a_persistent_session_and_streams_events() -> None:
    """subscribe() connects first, keys the session by subscriber id, and yields decoded events."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    minted = mint_subscriber_id("hero")
    subscriber_id, events = await source.subscribe(minted)
    assert subscriber_id == minted
    (client,) = _FakeClient.instances
    assert client.kwargs["identifier"] == f"crud-events-hero-{minted}"
    assert client.kwargs["clean_session"] is False
    assert client.subscribed == [("crud-events/hero", 1)]

    received = [event async for event in events]
    assert [event["action"] for event in received] == ["create", "delete"]
    await asyncio.gather(*list(_background_tasks))
    assert client.closed


async def test_source_durable_false_connects_with_a_clean_session() -> None:
    """durable=False asks the broker to drop the session when the connection ends."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    minted = mint_subscriber_id("hero")
    subscriber_id, events = await source.subscribe(minted, durable=False)
    assert subscriber_id == minted
    (client,) = _FakeClient.instances
    assert client.kwargs["identifier"] == f"crud-events-hero-{minted}"
    assert client.kwargs["clean_session"] is True
    assert client.subscribed == [("crud-events/hero", 1)]
    assert [event["action"] async for event in events] == ["create", "delete"]
    await asyncio.gather(*list(_background_tasks))
    assert client.closed


async def test_source_is_durable_by_default() -> None:
    """Omitting durable keeps the persistent session (clean_session=False)."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _, events = await source.subscribe(None)
    assert _FakeClient.instances[0].kwargs["clean_session"] is False
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_source_without_session_ttl_stays_on_mqtt_311() -> None:
    """session_ttl=None (the default) passes no v5 arguments, as in 0.4.0."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _, events = await source.subscribe(None)
    kwargs = _FakeClient.instances[0].kwargs
    assert not {"protocol", "clean_start", "properties"} & kwargs.keys()
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_durable_source_with_session_ttl_connects_over_v5_with_an_expiry() -> None:
    """A TTL makes the durable session MQTT 5.0, Clean Start 0, Session Expiry Interval=ttl."""
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=900
    )
    _, events = await source.subscribe(None)
    (client,) = _FakeClient.instances
    assert client.kwargs["protocol"] == "v5"
    assert client.kwargs["clean_start"] is False
    assert client.kwargs["properties"].SessionExpiryInterval == 900
    assert "clean_session" not in client.kwargs
    assert client.subscribed == [("crud-events/hero", 1)]
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_transient_source_ignores_session_ttl() -> None:
    """durable=False stays a 3.1.1 clean session even when session_ttl is set."""
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=900
    )
    _, events = await source.subscribe(None, durable=False)
    kwargs = _FakeClient.instances[0].kwargs
    assert kwargs["clean_session"] is True
    assert not {"protocol", "clean_start", "properties"} & kwargs.keys()
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


@pytest.mark.parametrize("ttl", [0, -1, 2**32, True, 3600.0])
def test_session_ttl_outside_the_expiry_interval_range_is_refused(ttl: object) -> None:
    """The interval is a four-byte unsigned int, and 0 would mean 'expire on disconnect'."""
    with pytest.raises(ValueError, match="session_ttl"):
        MQTTEventSource(
            hostname="b", port=1, resource="hero", keepalive=1, session_ttl=cast("int", ttl)
        )


async def test_source_issues_a_subscriber_id_when_none_is_given() -> None:
    """A first-time subscriber gets a fresh id back to reconnect with."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    subscriber_id, events = await source.subscribe(None)
    assert subscriber_id
    assert _FakeClient.instances[0].kwargs["identifier"].endswith(subscriber_id)
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_source_replaces_a_forged_or_foreign_subscriber_id() -> None:
    """A guessed id, or one minted for another resource, never names the broker session."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    for forged in ("abc", mint_subscriber_id("quest")):
        subscriber_id, events = await source.subscribe(forged)
        assert subscriber_id != forged
        assert _FakeClient.instances[-1].kwargs["identifier"] == f"crud-events-hero-{subscriber_id}"
        await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_aclose_before_the_first_step_disconnects_the_client() -> None:
    """An iterator closed without ever being iterated still releases its client."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _, events = await source.subscribe(None)
    (client,) = _FakeClient.instances
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))
    assert client.closed
    await events.aclose()  # idempotent


async def test_dropping_an_unstarted_iterator_disconnects_the_client() -> None:
    """Garbage collection releases the client of an iterator that was never iterated."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _, events = await source.subscribe(None)
    (client,) = _FakeClient.instances
    del events
    gc.collect()
    await asyncio.gather(*list(_background_tasks))
    assert client.closed


def test_dropping_an_iterator_after_its_loop_closed_warns_and_does_not_raise() -> None:
    """GC after the owning loop closed cannot disconnect; it warns, never raises."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    loop = asyncio.new_event_loop()
    try:
        _, events = loop.run_until_complete(source.subscribe(None))
    finally:
        loop.close()
    (client,) = _FakeClient.instances
    with pytest.warns(ResourceWarning, match="after its event loop closed"):
        del events
        gc.collect()
    assert not client.closed


async def test_aclose_while_a_step_is_running_still_releases_the_client() -> None:
    """A busy generator refuses aclose(); the release was already started regardless."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _, events = await source.subscribe(None)
    (client,) = _FakeClient.instances
    client.block = True
    step = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0)
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))
    assert client.closed
    step.cancel()
    await asyncio.gather(step, return_exceptions=True)


async def test_failed_subscribe_closes_the_client() -> None:
    """If SUBSCRIBE fails there is no iterator to own the client, so subscribe() closes it."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    _FakeClient.fail_subscribe = True
    try:
        with pytest.raises(OSError, match="refused"):
            await source.subscribe(None)
    finally:
        _FakeClient.fail_subscribe = False
    await asyncio.gather(*list(_background_tasks))
    assert _FakeClient.instances[0].closed


def test_dropping_an_iterator_outside_its_loop_disconnects_on_the_owning_loop() -> None:
    """GC on a thread with no running loop hands the disconnect to the loop that owns it."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    loop = asyncio.new_event_loop()
    try:
        _, events = loop.run_until_complete(source.subscribe(None))
        (client,) = _FakeClient.instances
        del events
        gc.collect()
        assert not client.closed  # only queued: the owning loop has not run yet
        loop.run_until_complete(asyncio.sleep(0.05))
        assert client.closed
    finally:
        loop.close()
