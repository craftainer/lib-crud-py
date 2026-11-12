"""Integration: events are published only after the real transaction commits."""

import uuid
from typing import Any

from craftainer_crud.interfaces.base import CRUDInterface, OwnerScope
from craftainer_crud.interfaces.delivery import drain_event_deliveries
from craftainer_crud.repositories.sqlalchemy import SQLAlchemyRepository
from tests.integration.db import session_factory
from tests.support.hero_model import Hero
from tests.support.hero_views import HeroV2, HeroV2Create


class _Sink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> None:  # noqa: ANN401 -- mirrors EventSink.publish
        self.calls.append(kwargs)


def _crud(session: Any, sink: _Sink, owner: str) -> CRUDInterface[HeroV2, Hero]:  # noqa: ANN401
    return CRUDInterface(
        schema=HeroV2,
        repository=SQLAlchemyRepository(session, Hero),
        owner=OwnerScope("owner_id", owner),
        events=sink,
        resource="hero",
    )


async def test_a_committed_write_is_published_after_the_commit() -> None:
    """Nothing is published while the transaction is open; the commit releases the event."""
    owner = f"events-{uuid.uuid4().hex}"
    sink = _Sink()
    async with session_factory() as session:
        crud = _crud(session, sink, owner)
        created = await crud.create(HeroV2Create(name="a", powers=["x"]))
        await drain_event_deliveries()
        assert sink.calls == []
        await session.commit()
        await drain_event_deliveries()
        assert [(c["record_id"], c["action"]) for c in sink.calls] == [(created.id, "create")]
        await crud.delete(created.id, expected_version=None)
        await session.commit()


async def test_a_rolled_back_write_publishes_nothing() -> None:
    """A real rollback drops the held events; a later commit of the session sends none."""
    owner = f"events-{uuid.uuid4().hex}"
    sink = _Sink()
    async with session_factory() as session:
        await _crud(session, sink, owner).create(HeroV2Create(name="a", powers=["x"]))
        await session.rollback()
        await session.commit()
    await drain_event_deliveries()
    assert sink.calls == []
