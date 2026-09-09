"""Unit test: build_repository_provider/build_event_sink_provider/build_event_source_provider's
MODE-aware selection.
"""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hero import Hero as HeroModel
from crud.interfaces import dependency as dependency_module
from crud.interfaces.base import InMemoryEventSink, MQTTEventSink, MQTTEventSource
from crud.interfaces.dependency import (
    build_event_sink_provider,
    build_event_source_provider,
    build_repository_provider,
)
from crud.repositories.memory import InMemoryRepository
from crud.repositories.sqlalchemy import SQLAlchemyRepository


def test_mock_mode_shares_one_repository_instance_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under MODE=mock, every call returns the same in-memory repository instance."""
    monkeypatch.setattr(dependency_module.settings, "mode", "mock")
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert first is second
    assert isinstance(first, InMemoryRepository)


def test_non_mock_mode_returns_a_fresh_sqlalchemy_repository_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside MODE=mock, each call returns a new SQLAlchemyRepository bound to the session."""
    monkeypatch.setattr(dependency_module.settings, "mode", "dev")
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert isinstance(first, SQLAlchemyRepository)
    assert isinstance(second, SQLAlchemyRepository)
    assert first is not second


def test_mock_mode_event_sink_and_source_share_one_instance_per_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under MODE=mock, a resource's sink and source providers return the same instance.

    Required for MODE=mock's delivery to work at all: a publish() call from the sink
    side must reach the subscribe()-registered queues on the source side -- see
    crud.interfaces.dependency's own `_mock_event_sink_for` docstring.
    """
    monkeypatch.setattr(dependency_module.settings, "mode", "mock")
    dependency_module._mock_event_sinks.clear()
    sink = build_event_sink_provider("widget")()
    source = build_event_source_provider("widget")()

    assert isinstance(sink, InMemoryEventSink)
    assert isinstance(source, InMemoryEventSink)
    assert sink is source


def test_non_mock_mode_returns_mqtt_backed_sink_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside MODE=mock, the sink/source providers return the real MQTT-backed adapters."""
    monkeypatch.setattr(dependency_module.settings, "mode", "dev")
    monkeypatch.setattr(dependency_module.settings, "mqtt_host", "mqtt-test-host")
    monkeypatch.setattr(dependency_module.settings, "mqtt_port", 18830)
    monkeypatch.setattr(dependency_module.settings, "mqtt_keepalive_seconds", 42)

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
