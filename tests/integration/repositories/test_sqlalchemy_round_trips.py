"""Integration test: SQLAlchemyRepository statement counts and fresh server timestamps.

Counts statements with a `before_cursor_execute` listener on the engine, inside one
uncommitted session (rolled back on close), so nothing is left behind.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.db import engine
from app.db import session_factory as async_session_factory
from app.models.hero import Hero
from craftainer_tool_crud.repositories.filtering import FilterClause, FilterOp
from craftainer_tool_crud.repositories.sqlalchemy import SQLAlchemyRepository

_BATCH = 5
_TAG = "round-trips-integration"


@contextmanager
def _count_statements() -> Iterator[list[str]]:
    statements: list[str] = []

    def _on_execute(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    target = engine.sync_engine
    event.listen(target, "before_cursor_execute", _on_execute)
    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", _on_execute)


def _selects(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("SELECT")]


async def test_create_has_fresh_timestamps_and_no_extra_select() -> None:
    """create is one INSERT ... RETURNING: timestamps are populated, no follow-up SELECT."""
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        with _count_statements() as statements:
            created = await repository.create({"name": _TAG, "powers": [], "owner_id": _TAG})
        assert created.created_at is not None
        assert created.updated_at is not None
        assert _selects(statements) == []


async def test_batch_operations_reload_once_and_return_fresh_updated_at() -> None:
    """update/delete/restore_many each cost one SELECT to find rows plus one reload."""
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        for index in range(_BATCH):
            await repository.create({"name": f"{_TAG}-{index}", "powers": [], "owner_id": _TAG})
        owner = [FilterClause("owner_id", FilterOp.EQ, _TAG)]

        with _count_statements() as statements:
            updated = await repository.update_many(filters=owner, data={"powers": ["x"]})
        assert len(updated) == _BATCH
        assert len(_selects(statements)) == 2  # find + one reload, independent of batch size
        assert all(hero.updated_at >= hero.created_at for hero in updated)

        with _count_statements() as statements:
            deleted = await repository.delete_many(filters=owner)
        assert len(deleted) == _BATCH
        assert len(_selects(statements)) == 2
        assert all(hero.archived_at is not None for hero in deleted)

        with _count_statements() as statements:
            restored = await repository.restore_many(filters=owner)
        assert len(restored) == _BATCH
        assert len(_selects(statements)) == 2
        assert all(hero.archived_at is None for hero in restored)
