"""Generic MODE-aware Repository/EventSink/EventSource selection, shared by every
resource's CRUD dependency.
"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from crud.interfaces.base import (
    EventSink,
    EventSource,
    InMemoryEventSink,
    MQTTEventSink,
    MQTTEventSource,
)
from crud.models.base import IdentifiedBase
from crud.repositories.base import Repository
from crud.repositories.memory import InMemoryRepository
from crud.repositories.sqlalchemy import SQLAlchemyRepository

settings = get_settings()

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
        _mock_event_sinks[resource] = InMemoryEventSink()
    return _mock_event_sinks[resource]


def build_repository_provider[ModelT: IdentifiedBase](
    model: type[ModelT],
) -> Callable[[AsyncSession], Repository[ModelT]]:
    """Return a per-request Repository[ModelT] provider: shared in-memory under
    MODE=mock, a fresh SQLAlchemyRepository bound to the request's session otherwise.
    """
    mock_repository: Repository[ModelT] = InMemoryRepository(model)

    def provider(session: AsyncSession) -> Repository[ModelT]:
        return mock_repository if settings.mode == "mock" else SQLAlchemyRepository(session, model)

    return provider


def build_event_sink_provider(resource: str) -> Callable[[], EventSink]:
    """Return an EventSink provider for `resource`: the shared InMemoryEventSink under
    MODE=mock, a fresh MQTTEventSink (stateless -- see its own docstring) otherwise.
    """

    def provider() -> EventSink:
        if settings.mode == "mock":
            return _mock_event_sink_for(resource)
        return MQTTEventSink(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            resource=resource,
            keepalive=settings.mqtt_keepalive_seconds,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            use_tls=settings.mqtt_use_tls,
        )

    return provider


def build_event_source_provider(resource: str) -> Callable[[], EventSource]:
    """Return an EventSource provider for `resource`, paired with build_event_sink_provider's
    sink for the same resource -- see `_mock_event_sink_for`'s own docstring for why
    MODE=mock needs the two to share one instance.
    """

    def provider() -> EventSource:
        if settings.mode == "mock":
            return _mock_event_sink_for(resource)
        return MQTTEventSource(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            resource=resource,
            keepalive=settings.mqtt_keepalive_seconds,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            use_tls=settings.mqtt_use_tls,
        )

    return provider
