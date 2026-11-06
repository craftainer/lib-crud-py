"""Unit test: MQTTEventSink/MQTTEventSource against a stand-in `aiomqtt` module (no broker)."""

import asyncio
import gc
import json
import ssl
import sys
import types
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from craftainer_crud.interfaces.base import (
    MQTTEventSink,
    MQTTEventSource,
    _background_tasks,
    _publishers,
    _SharedPublisher,
    close_event_publishers,
)
from craftainer_crud.interfaces.delivery import deliver_event
from craftainer_crud.interfaces.subscriber_ids import mint_subscriber_id

# A stand-in for the broker credential the sink forwards; not a real secret.
_FAKE_BROKER_CREDENTIAL = "p"
_FAKE_SECRET = "hunter2-stand-in"  # noqa: S105 -- a stand-in


class _Message:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()


class _FakeClient:
    """Records how it was constructed and used; replays two canned messages."""

    instances: list[_FakeClient] = []  # noqa: RUF012 -- test-only registry
    fail_subscribe = False
    fail_close = False
    fail_publish = 0  # publishes that raise before one succeeds

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
        if _FakeClient.fail_close:
            msg = "already gone"
            raise OSError(msg)

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        if _FakeClient.fail_publish:
            _FakeClient.fail_publish -= 1
            msg = "broker gone"
            raise OSError(msg)
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
    _FakeClient.fail_publish = 0
    _FakeClient.fail_close = False
    _publishers.clear()
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
        allow_insecure=True,
    )
    await sink.publish(resource="hero", record_id=7, action="create", snapshot={"name": "x"})
    (client,) = _FakeClient.instances
    assert client.kwargs["hostname"] == "broker"
    assert client.kwargs["port"] == 1884
    assert client.kwargs["username"] == "u"
    topic, payload, qos = client.published[0]
    assert (topic, qos) == ("crud-events/hero", 1)
    assert json.loads(payload)["record_id"] == 7
    assert not client.closed  # kept open for the next publish


async def test_source_resumes_a_persistent_session_and_streams_events() -> None:
    """subscribe() connects first, keys the session by subscriber id, and yields decoded events."""
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=None
    )
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
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=None
    )
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
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=None
    )
    _, events = await source.subscribe(None)
    assert _FakeClient.instances[0].kwargs["clean_session"] is False
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def test_source_without_session_ttl_stays_on_mqtt_311() -> None:
    """session_ttl=None passes no v5 arguments (the previous default)."""
    source = MQTTEventSource(
        hostname="broker", port=1883, resource="hero", keepalive=60, session_ttl=None
    )
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


def _sink(resource: str = "hero", **kwargs: Any) -> MQTTEventSink:  # noqa: ANN401
    return MQTTEventSink(hostname="broker", port=1883, resource=resource, keepalive=30, **kwargs)


async def _publish(sink: MQTTEventSink, record_id: int = 1) -> None:
    await sink.publish(resource=sink.resource, record_id=record_id, action="create", snapshot={})


async def test_sinks_with_the_same_broker_settings_share_one_connection() -> None:
    """A bulk write is one CONNECT: every publish, from any resource's sink, reuses it."""
    for record_id in range(3):
        await _publish(_sink("hero"), record_id)
    await _publish(_sink("quest"))
    (client,) = _FakeClient.instances
    assert [topic for topic, _, _ in client.published] == ["crud-events/hero"] * 3 + [
        "crud-events/quest"
    ]
    assert not client.closed


async def test_sinks_with_different_settings_do_not_share_a_connection() -> None:
    """Another host or user gets its own client."""
    await _publish(_sink())
    await _publish(MQTTEventSink(hostname="other", port=1883, resource="hero", keepalive=30))
    assert len(_FakeClient.instances) == 2


async def test_a_stale_shared_connection_is_replaced_and_the_publish_retried() -> None:
    """The broker may have dropped an idle client: reconnect once and publish again."""
    sink = _sink()
    await _publish(sink)
    _FakeClient.fail_publish = 1
    await _publish(sink, 2)
    stale, fresh = _FakeClient.instances
    assert stale.closed
    assert [json.loads(p)["record_id"] for _, p, _ in fresh.published] == [2]


async def test_a_failing_first_publish_is_raised_and_the_next_one_reconnects() -> None:
    """A fresh connection that fails is not retried; the client is dropped, the error raised."""
    sink = _sink()
    _FakeClient.fail_publish = 1
    with pytest.raises(OSError, match="broker gone"):
        await _publish(sink)
    await _publish(sink, 2)
    first, second = _FakeClient.instances
    assert first.closed
    assert [json.loads(p)["record_id"] for _, p, _ in second.published] == [2]


async def test_a_publish_that_fails_on_the_retry_too_is_raised() -> None:
    """Stale connection, and the replacement fails as well: raise, leave nothing open."""
    sink = _sink()
    await _publish(sink)
    _FakeClient.fail_publish = 2
    with pytest.raises(OSError, match="broker gone"):
        await _publish(sink, 2)
    assert all(client.closed for client in _FakeClient.instances)


async def test_a_cancelled_publish_discards_the_client() -> None:
    """A timeout or cancellation mid-publish leaves the client unusable: the next one reconnects."""
    sink = _sink()
    await _publish(sink)

    async def _hang(topic: str, payload: str, qos: int) -> None:
        await asyncio.Event().wait()

    _FakeClient.instances[0].publish = _hang  # type: ignore[method-assign]
    task = asyncio.ensure_future(_publish(sink, 2))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _FakeClient.instances[0].closed
    await _publish(sink, 3)
    assert len(_FakeClient.instances) == 2


async def test_close_event_publishers_waits_for_in_flight_deliveries() -> None:
    """Shutdown drains started after-commit deliveries before it disconnects."""
    sink = _sink()
    engine = create_engine("sqlite://")
    session = types.SimpleNamespace(sync_session=Session(engine))
    await deliver_event(
        sink, session=session, resource="hero", record_id=1, action="create", snapshot={}
    )
    session.sync_session.commit()
    session.sync_session.close()
    engine.dispose()
    await close_event_publishers()
    (client,) = _FakeClient.instances
    assert len(client.published) == 1
    assert client.closed


async def test_a_failing_disconnect_is_logged_not_raised() -> None:
    """Discarding a broken client tolerates its disconnect failing too."""
    sink = _sink()
    await _publish(sink)
    _FakeClient.fail_close = True
    _FakeClient.fail_publish = 1
    await _publish(sink, 2)
    assert len(_FakeClient.instances) == 2


async def test_close_event_publishers_disconnects_and_a_later_publish_reconnects() -> None:
    """Shutdown closes the shared clients; closing an unconnected publisher is a no-op."""
    sink = _sink()
    await _publish(sink)
    _publishers[("never-connected",)] = _SharedPublisher(sink)
    await close_event_publishers()
    (client,) = _FakeClient.instances
    assert client.closed
    assert not _publishers
    await _publish(sink)
    assert len(_FakeClient.instances) == 2


def test_the_shared_connection_is_per_event_loop() -> None:
    """aiomqtt clients bind to their loop, so another loop opens its own."""
    sink = _sink()
    for _ in range(2):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_publish(sink))
        finally:
            loop.close()
    assert len(_FakeClient.instances) == 2


def test_the_sink_and_source_refuse_credentials_without_tls() -> None:
    """The same checks as MQTTConfig apply to a hand-built sink or source."""
    with pytest.raises(ValueError, match="clear text"):
        _sink(username="u")
    with pytest.raises(ValueError, match="clear text"):
        MQTTEventSource(
            hostname="b", port=1, resource="hero", keepalive=1, password=_FAKE_BROKER_CREDENTIAL
        )
    assert _FAKE_SECRET not in repr(_sink(password=_FAKE_SECRET, use_tls=True))


async def test_tls_options_reach_the_client_as_one_verifying_context() -> None:
    """use_tls hands aiomqtt the caller's context (sink and source alike); none, no TLS."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    await _publish(_sink(use_tls=True, tls_context=context))
    source = MQTTEventSource(
        hostname="b", port=8883, resource="hero", keepalive=1, use_tls=True, tls_context=context
    )
    _, events = await source.subscribe(None)
    plain = await _sink_client_kwargs()
    assert [c.kwargs.get("tls_context") for c in _FakeClient.instances[:2]] == [context, context]
    assert "tls_context" not in plain
    await events.aclose()
    await asyncio.gather(*list(_background_tasks))


async def _sink_client_kwargs() -> dict[str, Any]:
    await _publish(_sink("plain"))
    return _FakeClient.instances[-1].kwargs
