"""Unit test: build_repository_provider/build_event_sink_provider/build_event_source_provider's
MODE-aware selection.
"""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from craftainer_crud.interfaces import dependency
from craftainer_crud.interfaces.base import InMemoryEventSink, MQTTEventSink, MQTTEventSource
from craftainer_crud.interfaces.dependency import (
    build_event_sink_provider,
    build_event_source_provider,
    build_repository_provider,
)
from craftainer_crud.repositories.memory import InMemoryRepository
from craftainer_crud.repositories.sqlalchemy import SQLAlchemyRepository
from tests.support.hero_model import Hero as HeroModel
from tests.unit.conftest import CrudConfigOverride


def test_mock_mode_shares_one_repository_instance_across_calls(
    override_crud_config: CrudConfigOverride,
) -> None:
    """Under MODE=mock, every call returns the same in-memory repository instance."""
    override_crud_config(use_memory=True)
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert first is second
    assert isinstance(first, InMemoryRepository)


def test_non_mock_mode_returns_a_fresh_sqlalchemy_repository_per_call(
    override_crud_config: CrudConfigOverride,
) -> None:
    """Outside MODE=mock, each call returns a new SQLAlchemyRepository bound to the session."""
    override_crud_config(use_memory=False)
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert isinstance(first, SQLAlchemyRepository)
    assert isinstance(second, SQLAlchemyRepository)
    assert first is not second


def test_mock_mode_event_sink_and_source_share_one_instance_per_resource(
    override_crud_config: CrudConfigOverride,
) -> None:
    """Under MODE=mock, a resource's sink and source providers return the same instance.

    Required for MODE=mock's delivery to work at all: a publish() call from the sink
    side must reach the subscribe()-registered queues on the source side -- see
    craftainer_crud.interfaces.dependency's own `_mock_event_sink_for` docstring.
    """
    override_crud_config(use_memory=True)
    dependency._mock_event_sinks.clear()
    sink = build_event_sink_provider("widget")()
    source = build_event_source_provider("widget")()

    assert isinstance(sink, InMemoryEventSink)
    assert isinstance(source, InMemoryEventSink)
    assert sink is source


def test_mock_mode_gives_each_resource_label_its_own_scoped_sink(
    override_crud_config: CrudConfigOverride,
) -> None:
    """Different labels get different sinks, each scoped to its own label."""
    override_crud_config(use_memory=True)
    dependency._mock_event_sinks.clear()
    widget = build_event_sink_provider("widget")()
    gadget = build_event_sink_provider("gadget")()

    assert isinstance(widget, InMemoryEventSink)
    assert isinstance(gadget, InMemoryEventSink)
    assert widget is not gadget
    assert widget._resource == "widget"
    assert gadget._resource == "gadget"


def test_non_mock_mode_returns_mqtt_backed_sink_and_source(
    override_crud_config: CrudConfigOverride,
) -> None:
    """Outside MODE=mock, the sink/source providers return the real MQTT-backed adapters."""
    override_crud_config(
        use_memory=False, mqtt={"host": "mqtt-test-host", "port": 18830, "keepalive_seconds": 42}
    )

    sink = build_event_sink_provider("widget")()
    source = build_event_source_provider("widget")()

    assert isinstance(sink, MQTTEventSink)
    assert sink == MQTTEventSink(
        hostname="mqtt-test-host", port=18830, resource="widget", keepalive=42
    )
    assert isinstance(source, MQTTEventSource)
    assert source == MQTTEventSource(
        hostname="mqtt-test-host", port=18830, resource="widget", keepalive=42
    )
