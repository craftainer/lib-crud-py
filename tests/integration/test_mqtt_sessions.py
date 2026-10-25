"""Integration: transient vs durable subscriber sessions against the devcontainer's broker."""

import asyncio
import gc
import json
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, cast

import aiomqtt
import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from craftainer_crud.controllers import crud_router as crud_router_module
from craftainer_crud.interfaces.base import MQTTEventSink, MQTTEventSource, _background_tasks

if TYPE_CHECKING:
    from fastapi import Request

_HOST = os.environ.get("MQTT_HOST")
_PORT = int(os.environ.get("MQTT_PORT", "1883"))

pytestmark = pytest.mark.skipif(not _HOST, reason="needs a broker: MQTT_HOST not set")


async def _queued_after_reconnect(resource: str, subscriber_id: str) -> int:
    """Count messages a persistent reconnect receives without resubscribing.

    The broker only has something to deliver if it kept the session (and its
    subscription) while the subscriber was away. The probe then connects once more with a
    clean session so it leaves nothing behind.
    """
    assert _HOST
    identifier = f"crud-events-{resource}-{subscriber_id}"
    count = 0
    async with aiomqtt.Client(
        hostname=_HOST, port=_PORT, identifier=identifier, clean_session=False
    ) as probe:
        try:
            async with asyncio.timeout(1.5):
                async for _ in probe.messages:
                    count += 1
        except TimeoutError:
            pass
    async with aiomqtt.Client(
        hostname=_HOST, port=_PORT, identifier=identifier, clean_session=True
    ):
        pass
    return count


async def _publish_while_disconnected(durable: bool) -> int:
    assert _HOST
    resource = f"session-{uuid.uuid4().hex}"
    source = MQTTEventSource(hostname=_HOST, port=_PORT, resource=resource, keepalive=30)
    sink = MQTTEventSink(hostname=_HOST, port=_PORT, resource=resource, keepalive=30)
    subscriber_id, events = await source.subscribe(None, durable=durable)
    await sink.publish(resource=resource, record_id=0, action="create", snapshot={})
    async with asyncio.timeout(5):
        assert (await anext(events))["record_id"] == 0  # delivered while connected
    # Cancelling a started iterator is what an ended SSE stream does; it runs the
    # generator's `finally`, which disconnects.
    consumer = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0.3)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await asyncio.gather(*list(_background_tasks))
    await sink.publish(resource=resource, record_id=1, action="create", snapshot={})
    return await _queued_after_reconnect(resource, subscriber_id)


async def test_transient_subscriber_leaves_no_broker_session() -> None:
    """After a durable=False subscriber disconnects, the broker holds nothing for its id."""
    assert await _publish_while_disconnected(durable=False) == 0


async def test_durable_subscriber_keeps_its_broker_session() -> None:
    """The default (durable) subscriber still has its queued message waiting on reconnect."""
    assert await _publish_while_disconnected(durable=True) == 1


class _Probe:
    """A raw paho MQTT 5.0 client that reconnects to a durable session and reports what it finds."""

    def __init__(self, identifier: str) -> None:
        self.session_present: bool | None = None
        self.received: list[bytes] = []
        self._connected = threading.Event()
        self._client = paho.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=identifier,
            protocol=paho.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = lambda _c, _u, msg: self.received.append(msg.payload)

    def _on_connect(self, _c: object, _u: object, flags: paho.ConnectFlags, *_rest: object) -> None:
        self.session_present = flags.session_present
        self._connected.set()

    def reconnect(self, *, clean_start: bool) -> None:
        """Connect (no SUBSCRIBE), wait for the queued replay, then DISCONNECT."""
        assert _HOST
        props = cast("Callable[[int], Properties]", Properties)(PacketTypes.CONNECT)
        props.SessionExpiryInterval = 0
        self._client.connect(_HOST, _PORT, 30, clean_start=clean_start, properties=props)
        self._client.loop_start()
        try:
            assert self._connected.wait(5)
            time.sleep(1.0)  # let any queued QoS 1 messages arrive
        finally:
            self._client.disconnect()
            self._client.loop_stop()


def _identifier(resource: str, subscriber_id: str) -> str:
    return f"crud-events-{resource}-{subscriber_id}"


async def _discard(resource: str, subscriber_id: str) -> None:
    """Clean Start 1 with expiry 0 discards whatever session the id still has."""
    probe = _Probe(_identifier(resource, subscriber_id))
    await asyncio.to_thread(probe.reconnect, clean_start=True)


async def _ttl_source_leaves_session(session_ttl: int | None) -> tuple[str, str]:
    """Start a durable subscriber, read one event, disconnect it, publish a second event."""
    assert _HOST
    resource = f"ttl-{uuid.uuid4().hex}"
    source = MQTTEventSource(
        hostname=_HOST, port=_PORT, resource=resource, keepalive=30, session_ttl=session_ttl
    )
    sink = MQTTEventSink(hostname=_HOST, port=_PORT, resource=resource, keepalive=30)
    subscriber_id, events = await source.subscribe(None)
    await sink.publish(resource=resource, record_id=0, action="create", snapshot={})
    async with asyncio.timeout(5):
        # A v3 publisher reaches a v5 subscriber on the same topic.
        assert (await anext(events))["record_id"] == 0
    consumer = asyncio.ensure_future(anext(events))
    await asyncio.sleep(0.3)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await asyncio.gather(*list(_background_tasks))
    await sink.publish(resource=resource, record_id=1, action="create", snapshot={})
    return resource, subscriber_id


async def test_session_ttl_session_replays_within_the_ttl() -> None:
    """Back inside the TTL, the v5 session is present and holds the event published meanwhile."""
    resource, subscriber_id = await _ttl_source_leaves_session(session_ttl=30)
    try:
        probe = _Probe(_identifier(resource, subscriber_id))
        await asyncio.to_thread(probe.reconnect, clean_start=False)
        assert probe.session_present is True
        assert len(probe.received) == 1
        assert json.loads(probe.received[0])["record_id"] == 1
    finally:
        await _discard(resource, subscriber_id)


async def test_session_ttl_session_is_gone_after_the_ttl() -> None:
    """Past the TTL the broker has dropped the session: not present, nothing queued."""
    resource, subscriber_id = await _ttl_source_leaves_session(session_ttl=2)
    try:
        await asyncio.sleep(5)
        probe = _Probe(_identifier(resource, subscriber_id))
        await asyncio.to_thread(probe.reconnect, clean_start=False)
        assert probe.session_present is False
        assert probe.received == []
    finally:
        await _discard(resource, subscriber_id)


async def test_without_session_ttl_the_session_survives_the_short_wait() -> None:
    """session_ttl=None is the 0.4.0 behaviour: the (v3) session is still there after 5s."""
    resource, subscriber_id = await _ttl_source_leaves_session(session_ttl=None)
    await asyncio.sleep(5)
    assert await _queued_after_reconnect(resource, subscriber_id) == 1


async def _subscribed_client(
    durable: bool,
) -> tuple[MQTTEventSource, AsyncIterator[dict[str, object]], aiomqtt.Client]:
    """Subscribe against the broker, capturing the aiomqtt client the source builds."""
    assert _HOST
    created: list[aiomqtt.Client] = []
    original = aiomqtt.Client.__init__

    def spy(self: aiomqtt.Client, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original(self, *args, **kwargs)
        created.append(self)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(aiomqtt.Client, "__init__", spy)
        source = MQTTEventSource(
            hostname=_HOST, port=_PORT, resource=f"leak-{uuid.uuid4().hex}", keepalive=30
        )
        _, events = await source.subscribe(None, durable=durable)
    (client,) = created
    assert client._client.is_connected()
    return source, events, client


async def _assert_disconnected(client: aiomqtt.Client) -> None:
    await asyncio.gather(*list(_background_tasks))
    assert not client._client.is_connected()


async def test_unstarted_iterator_is_released_by_aclose() -> None:
    """aclose() before the first step disconnects the client and stops its network loop."""
    _, events, client = await _subscribed_client(durable=False)
    await events.aclose()  # type: ignore[attr-defined]
    await _assert_disconnected(client)


async def test_unstarted_iterator_is_released_by_garbage_collection() -> None:
    """Dropping the iterator without iterating it disconnects the client."""
    _, events, client = await _subscribed_client(durable=True)
    del events
    gc.collect()
    await _assert_disconnected(client)


async def test_sse_events_cancelled_before_the_first_event_releases_the_client() -> None:
    """The SSE stream, cancelled right after its first frame, leaves no client connected."""
    _, events, client = await _subscribed_client(durable=False)
    stream = cast(
        "AsyncGenerator[str]",
        crud_router_module._sse_events(cast("Request", _NeverDisconnected()), "sub", events),
    )
    assert (await anext(stream)).startswith("id: 0")
    await stream.aclose()
    await _assert_disconnected(client)


class _NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False
