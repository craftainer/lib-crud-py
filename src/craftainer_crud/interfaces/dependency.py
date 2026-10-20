"""Generic MODE-aware Repository/EventSink/EventSource selection, shared by every
resource's CRUD dependency.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from craftainer_crud.config import MQTTConfig, get_config
from craftainer_crud.interfaces.base import (
    EventSink,
    EventSource,
    InMemoryEventSink,
    MQTTEventSink,
    MQTTEventSource,
)
from craftainer_crud.models.base import IdentifiedBase
from craftainer_crud.repositories.base import Repository
from craftainer_crud.repositories.memory import InMemoryRepository
from craftainer_crud.repositories.sqlalchemy import SQLAlchemyRepository

# One InMemoryEventSink per resource, built lazily and shared by both
# build_event_sink_provider and build_event_source_provider for that resource --
# InMemoryEventSink satisfies both EventSink and EventSource (see its own
# docstring), so a publish() call from a sink built for "hero" must reach the
# subscribe()-registered queues of a source built for "hero" too. Keyed by
# resource name (rather than a closure-local variable like build_repository_
# provider's `mock_repository`) specifically because the sink and source sides
# are built from two separate calls to two separate functions below, unlike
# build_repository_provider's single provider serving one call site.
_mock_event_sinks: dict[str, InMemoryEventSink] = {}


def _mock_event_sink_for(resource: str) -> InMemoryEventSink:
    """Return the shared InMemoryEventSink for `resource`, creating it on first use."""
    if resource not in _mock_event_sinks:
        _mock_event_sinks[resource] = InMemoryEventSink(resource=resource)
    return _mock_event_sinks[resource]


def _mqtt_kwargs(mqtt: MQTTConfig, resource: str) -> dict[str, Any]:
    """Map an MQTTConfig onto MQTTEventSink/MQTTEventSource's shared constructor kwargs."""
    return {
        "hostname": mqtt.host,
        "port": mqtt.port,
        "resource": resource,
        "keepalive": mqtt.keepalive_seconds,
        "username": mqtt.username,
        "password": mqtt.password,
        "use_tls": mqtt.use_tls,
    }


def build_repository_provider[ModelT: IdentifiedBase](
    model: type[ModelT],
) -> Callable[[AsyncSession], Repository[ModelT]]:
    """Return a per-request Repository[ModelT] provider: shared in-memory in
    mock mode (CrudConfig.use_memory), a fresh SQLAlchemyRepository bound to the request's
    session otherwise.
    """
    mock_repository: Repository[ModelT] = InMemoryRepository(model)

    def provider(session: AsyncSession) -> Repository[ModelT]:
        return mock_repository if get_config().use_memory else SQLAlchemyRepository(session, model)

    return provider


def build_event_sink_provider(resource: str) -> Callable[[], EventSink]:
    """Return an EventSink provider for `resource`: the shared InMemoryEventSink in
    mock mode (CrudConfig.use_memory), a fresh MQTTEventSink (stateless -- see its own
    docstring) otherwise.
    """

    def provider() -> EventSink:
        config = get_config()
        if config.use_memory:
            return _mock_event_sink_for(resource)
        return MQTTEventSink(
            **_mqtt_kwargs(config.mqtt, resource),
        )

    return provider


def build_event_source_provider(resource: str) -> Callable[[], EventSource]:
    """Return an EventSource provider for `resource`, paired with build_event_sink_provider's
    sink for the same resource -- see `_mock_event_sink_for`'s own docstring for why
    mock mode (CrudConfig.use_memory) needs the two to share one instance.
    """

    def provider() -> EventSource:
        config = get_config()
        if config.use_memory:
            return _mock_event_sink_for(resource)
        return MQTTEventSource(
            **_mqtt_kwargs(config.mqtt, resource),
        )

    return provider
