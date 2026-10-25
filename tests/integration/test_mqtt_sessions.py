"""Integration: transient vs durable subscriber sessions against the devcontainer's broker."""

import asyncio
import os
import uuid

import aiomqtt
import pytest

from craftainer_crud.interfaces.base import MQTTEventSink, MQTTEventSource, _background_tasks

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
