"""Unit test: MQTTEventSink/MQTTEventSource against a stand-in `aiomqtt` module (no broker)."""

import asyncio
import json
import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from craftainer_tool_crud.interfaces.base import MQTTEventSink, MQTTEventSource, _background_tasks

# A stand-in for the broker credential the sink forwards; not a real secret.
_FAKE_BROKER_CREDENTIAL = "p"


class _Message:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode()


class _FakeClient:
    """Records how it was constructed and used; replays two canned messages."""

    instances: list[_FakeClient] = []  # noqa: RUF012 -- test-only registry

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 -- mirrors aiomqtt.Client(**kwargs)
        self.kwargs = kwargs
        self.published: list[tuple[str, str, int]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.closed = False
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def publish(self, topic: str, payload: str, qos: int) -> None:
        self.published.append((topic, payload, qos))

    async def subscribe(self, topic: str, qos: int) -> None:
        self.subscribed.append((topic, qos))

    @property
    def messages(self) -> AsyncIterator[_Message]:
        async def _replay() -> AsyncIterator[_Message]:
            yield _Message({"action": "create", "record_id": 1})
            yield _Message({"action": "delete", "record_id": 1})

        return _replay()


@pytest.fixture(autouse=True)
def _fake_aiomqtt(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.instances.clear()
    module = types.ModuleType("aiomqtt")
    module.Client = _FakeClient  # type: ignore[attr-defined]
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
    subscriber_id, events = await source.subscribe("abc")
    assert subscriber_id == "abc"
    (client,) = _FakeClient.instances
    assert client.kwargs["identifier"] == "crud-events-hero-abc"
    assert client.kwargs["clean_session"] is False
    assert client.subscribed == [("crud-events/hero", 1)]

    received = [event async for event in events]
    assert [event["action"] for event in received] == ["create", "delete"]
    await asyncio.gather(*list(_background_tasks))
    assert client.closed


async def test_source_issues_a_subscriber_id_when_none_is_given() -> None:
    """A first-time subscriber gets a fresh id back to reconnect with."""
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    subscriber_id, events = await source.subscribe(None)
    assert subscriber_id
    assert _FakeClient.instances[0].kwargs["identifier"].endswith(subscriber_id)
    await events.aclose()  # type: ignore[attr-defined]
    await asyncio.gather(*list(_background_tasks))
