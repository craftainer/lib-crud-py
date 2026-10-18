"""Unit test: SQLAlchemyRepository's client-side field validation, no DB needed.

`_where_clauses`/`_order_by` (via `_column`) only ever inspect the bound model
class's own mapper -- they never touch `self._session` -- so this can be tested
synchronously with no session at all, unlike every other SQLAlchemyRepository
behavior (see tests/integration/repositories/test_sqlalchemy.py for those, which
need a real Postgres session).
"""

from unittest.mock import AsyncMock, MagicMock, _Call

import pytest

from app.models.hero import Hero
from craftainer_tool_crud.repositories.filtering import FilterClause, FilterOp, SortClause
from craftainer_tool_crud.repositories.sqlalchemy import SQLAlchemyRepository


def test_where_clauses_rejects_unknown_filter_field() -> None:
    """A filter field that isn't a real mapped column raises, not an AttributeError."""
    repository = SQLAlchemyRepository(session=None, model=Hero)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a filterable/sortable column"):
        repository._where_clauses([FilterClause("not_a_real_field", FilterOp.EQ, "x")])


def test_order_by_rejects_unknown_sort_field() -> None:
    """A sort field that isn't a real mapped column raises, not an AttributeError."""
    repository = SQLAlchemyRepository(session=None, model=Hero)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a filterable/sortable column"):
        repository._order_by([SortClause("not_a_real_field")])


class _FakeInstance:
    """Stand-in for a mapped row: only `id` matters to `_reload`."""

    def __init__(self, record_id: int) -> None:
        self.id = record_id


def _repository_with_scalars(
    instances: list[_FakeInstance],
) -> tuple[SQLAlchemyRepository[Hero], AsyncMock]:
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = instances
    session.execute.return_value = result
    return SQLAlchemyRepository(session, Hero), session


def _is_reload(call: _Call) -> bool:
    statement = call.args[0]
    return bool(statement.get_execution_options().get("populate_existing"))


async def test_reload_skips_empty_batch() -> None:
    """No instances means no reload statement at all."""
    repository, session = _repository_with_scalars([])
    await repository._reload([])
    session.execute.assert_not_called()


async def test_reload_is_one_populate_existing_select_for_the_batch() -> None:
    """A batch is re-read with a single `id IN (...)` SELECT using populate_existing."""
    repository, session = _repository_with_scalars([])
    await repository._reload([_FakeInstance(1), _FakeInstance(2), _FakeInstance(3)])  # type: ignore[list-item]
    session.execute.assert_awaited_once()
    assert _is_reload(session.execute.await_args)
    assert " IN " in str(session.execute.await_args.args[0])


@pytest.mark.parametrize("operation", ["update_many", "delete_many", "restore_many"])
async def test_many_operations_reload_once_regardless_of_batch_size(operation: str) -> None:
    """update/delete/restore_many issue one SELECT to find rows plus one reload, never per row."""
    instances = [_FakeInstance(n) for n in range(5)]
    repository, session = _repository_with_scalars(instances)
    if operation == "update_many":
        await repository.update_many(filters=[], data={"name": "x"})
    else:
        await getattr(repository, operation)(filters=[])
    reloads = [call for call in session.execute.await_args_list if _is_reload(call)]
    assert len(reloads) == 1
    assert session.execute.await_count == 2
    session.refresh.assert_not_called()


async def test_create_does_not_refresh() -> None:
    """create relies on INSERT ... RETURNING for server defaults, so no extra statement."""
    repository, session = _repository_with_scalars([])
    await repository.create({"name": "x", "powers": [], "owner_id": "o"})
    session.flush.assert_awaited_once()
    session.refresh.assert_not_called()
    session.execute.assert_not_called()
