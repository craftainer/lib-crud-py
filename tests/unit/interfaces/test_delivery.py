"""Unit test: after-commit, best-effort event delivery (craftainer_crud.interfaces.delivery)."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from craftainer_crud.interfaces.base import CRUDInterface
from craftainer_crud.interfaces.delivery import (
    deliver_event,
    drain_event_deliveries,
    event_delivery_stats,
)
from tests.unit.interfaces.test_base import (
    _FakeWidgetRepository,
    _Widget,
    _WidgetCreate,
    _WidgetRecord,
)


class _Sink:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def publish(self, **kwargs: Any) -> None:  # noqa: ANN401 -- mirrors EventSink.publish
        if self.fail:
            msg = "broker down"
            raise OSError(msg)
        self.calls.append(kwargs)


class _SessionRepository(_FakeWidgetRepository):
    """The in-memory fake, but bound to a session like SQLAlchemyRepository."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__()
        self.session = session


def _event(record_id: int = 1) -> dict[str, Any]:
    return {"resource": "widget", "record_id": record_id, "action": "create", "snapshot": {}}


async def test_without_a_session_the_event_is_published_at_once() -> None:
    """No transaction to wait for (the in-memory repository): publish immediately."""
    sink = _Sink()
    await deliver_event(sink, session=None, **_event())
    assert sink.calls == [_event()]


async def test_a_broker_failure_is_logged_and_counted_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing sink must not turn a successful write into an error."""
    before = event_delivery_stats()
    with caplog.at_level(logging.WARNING):
        await deliver_event(_Sink(fail=True), session=None, **_event())
    after = event_delivery_stats()
    assert after.failed == before.failed + 1
    assert after.published == before.published
    assert "event publish failed" in caplog.text


async def test_events_wait_for_the_commit_and_publish_in_order() -> None:
    """Nothing is published while the transaction is open; the commit releases them in order."""
    sink = _Sink()
    session = AsyncSession()
    before = event_delivery_stats()
    await deliver_event(sink, session=session, **_event(1))
    await deliver_event(sink, session=session, **_event(2))
    await drain_event_deliveries()
    assert sink.calls == []
    await session.commit()
    await drain_event_deliveries()
    assert [call["record_id"] for call in sink.calls] == [1, 2]
    assert event_delivery_stats().published == before.published + 2
    # The listeners are registered once per session: a second batch is not delivered twice.
    await deliver_event(sink, session=session, **_event(3))
    await session.commit()
    await session.commit()  # nothing pending: no task
    await drain_event_deliveries()
    assert [call["record_id"] for call in sink.calls] == [1, 2, 3]


class _Bound:
    """What `deliver_event` needs of an AsyncSession, over a real sync session."""

    def __init__(self) -> None:
        self.engine = create_engine("sqlite://")
        self.sync_session = Session(self.engine)


@pytest.fixture
def bound() -> Iterator[_Bound]:
    """A sync session over in-memory SQLite, closed afterwards."""
    holder = _Bound()
    yield holder
    holder.sync_session.close()
    holder.engine.dispose()


async def _flush(sink: _Sink) -> list[int]:
    await drain_event_deliveries()
    return [call["record_id"] for call in sink.calls]


async def test_a_rolled_back_transaction_publishes_nothing(bound: _Bound) -> None:
    """A rollback drops the held events, so a later commit does not release them."""
    sink = _Sink()
    bound.sync_session.execute(text("select 1"))
    await deliver_event(sink, session=bound, **_event())
    bound.sync_session.rollback()
    bound.sync_session.commit()
    assert await _flush(sink) == []


async def test_closing_without_a_commit_drops_the_events(bound: _Bound) -> None:
    """A session reused after close() must not publish the abandoned transaction's events."""
    sink = _Sink()
    bound.sync_session.execute(text("select 1"))
    await deliver_event(sink, session=bound, **_event(1))
    bound.sync_session.close()
    await deliver_event(sink, session=bound, **_event(2))
    bound.sync_session.commit()
    assert await _flush(sink) == [2]


async def test_a_savepoint_release_does_not_publish_before_the_outer_commit(bound: _Bound) -> None:
    """Only the outermost commit publishes; a rolled back outer transaction publishes nothing."""
    sink = _Sink()
    session = bound.sync_session
    session.execute(text("select 1"))
    savepoint = session.begin_nested()
    await deliver_event(sink, session=bound, **_event(1))
    savepoint.commit()
    assert await _flush(sink) == []
    session.rollback()
    assert await _flush(sink) == []
    await deliver_event(sink, session=bound, **_event(2))
    session.commit()
    assert await _flush(sink) == [2]


async def test_a_savepoint_rollback_drops_only_the_events_recorded_inside_it(bound: _Bound) -> None:
    """Earlier events survive; events in the failed savepoint, nested ones included, do not."""
    sink = _Sink()
    session = bound.sync_session
    session.execute(text("select 1"))
    await deliver_event(sink, session=bound, **_event(1))
    outer = session.begin_nested()
    await deliver_event(sink, session=bound, **_event(2))
    inner = session.begin_nested()
    await deliver_event(sink, session=bound, **_event(3))
    inner.commit()
    outer.rollback()
    await deliver_event(sink, session=bound, **_event(4))
    session.commit()
    assert await _flush(sink) == [1, 4]


async def test_a_failing_sink_after_commit_does_not_fail_the_commit() -> None:
    """The commit has happened; a broker failure afterwards is only counted."""
    session = AsyncSession()
    before = event_delivery_stats()
    await deliver_event(_Sink(fail=True), session=session, **_event())
    await session.commit()
    await drain_event_deliveries()
    assert event_delivery_stats().failed == before.failed + 1


async def test_crud_interface_defers_events_to_the_repository_session() -> None:
    """CRUDInterface finds the session on its repository and publishes after the commit."""
    sink = _Sink()
    session = AsyncSession()
    crud: CRUDInterface[_Widget, _WidgetRecord] = CRUDInterface(
        schema=_Widget,
        repository=_SessionRepository(session),
        events=sink,
        resource="widget",
    )
    created = await crud.create(_WidgetCreate(label="a"))
    await drain_event_deliveries()
    assert sink.calls == []
    await session.commit()
    await drain_event_deliveries()
    assert [call["record_id"] for call in sink.calls] == [created.id]
