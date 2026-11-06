"""After-commit, best-effort delivery of record events.

`CRUDInterface` hands each event to `deliver_event` instead of awaiting `EventSink.publish`
inside the request. For a repository bound to a SQLAlchemy session the event is held on the
session and published only once that session's transaction commits (SQLAlchemy's
`after_commit` event), in a background task, in order; a rollback, or a session that is
closed without committing, drops it, so a subscriber never sees an event for a change that
did not happen. A repository without a session (the in-memory one) has no transaction, so
its events are published straight away.

Delivery is best effort: a sink that raises is logged and counted
(`event_delivery_stats().failed`), never surfaced to the request, and an event is not
retried. A savepoint rollback inside the transaction does not retract the events recorded
before it; the library opens no savepoints itself.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from craftainer_crud.interfaces.base import EventSink

logger = logging.getLogger(__name__)

_PENDING_KEY = "craftainer_crud.pending_events"

# Strong references: asyncio keeps only a weak reference to a running task, so a
# fire-and-forget delivery would otherwise be collectable mid-publish.
_delivery_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class EventDeliveryStats:
    """A snapshot of how many events this process published and failed to publish."""

    published: int
    failed: int


_published = 0
_failed = 0


def event_delivery_stats() -> EventDeliveryStats:
    """Return the process-wide delivery counters (for a metrics gauge or a health check)."""
    return EventDeliveryStats(published=_published, failed=_failed)


_PendingEvent = tuple["EventSink", dict[str, Any]]


async def _publish_best_effort(sink: EventSink, kwargs: dict[str, Any]) -> None:
    """Publish one event; log and count a failure instead of raising it."""
    global _published, _failed  # noqa: PLW0603 -- process-wide counters
    try:
        await sink.publish(**kwargs)
    except Exception:
        _failed += 1
        logger.warning(
            "event publish failed (resource=%s, record_id=%s, action=%s)",
            kwargs.get("resource"),
            kwargs.get("record_id"),
            kwargs.get("action"),
            exc_info=True,
        )
    else:
        _published += 1


async def _publish_all(events: list[_PendingEvent]) -> None:
    for sink, kwargs in events:
        await _publish_best_effort(sink, kwargs)


def _on_commit(sync_session: Any) -> None:  # noqa: ANN401 -- SQLAlchemy's sync Session
    pending: list[_PendingEvent] = sync_session.info.get(_PENDING_KEY, [])
    if not pending:
        return
    events = list(pending)
    pending.clear()
    task = asyncio.get_running_loop().create_task(_publish_all(events))
    _delivery_tasks.add(task)
    task.add_done_callback(_delivery_tasks.discard)


def _on_rollback(sync_session: Any) -> None:  # noqa: ANN401 -- SQLAlchemy's sync Session
    sync_session.info.get(_PENDING_KEY, []).clear()


async def deliver_event(
    sink: EventSink,
    *,
    session: object | None,
    **kwargs: Any,  # noqa: ANN401 -- the EventSink.publish keywords
) -> None:
    """Publish `kwargs` to `sink` after `session` commits, or now when there is no session."""
    if session is None:
        await _publish_best_effort(sink, kwargs)
        return
    sync_session = cast("AsyncSession", session).sync_session
    pending = sync_session.info.get(_PENDING_KEY)
    if pending is None:
        pending = sync_session.info[_PENDING_KEY] = []
        event.listen(sync_session, "after_commit", _on_commit)
        event.listen(sync_session, "after_rollback", _on_rollback)
    pending.append((sink, kwargs))


async def drain_event_deliveries() -> None:
    """Wait for every after-commit delivery started so far (tests, graceful shutdown)."""
    while _delivery_tasks:
        await asyncio.gather(*_delivery_tasks, return_exceptions=True)
