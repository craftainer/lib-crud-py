"""Unit test: SQLAlchemyRepository's client-side field validation, no DB needed.

`_where_clauses`/`_order_by` (via `_column`) only ever inspect the bound model
class's own mapper -- they never touch `self._session` -- so this can be tested
synchronously with no session at all, unlike every other SQLAlchemyRepository
behavior (see tests/integration/repositories/test_sqlalchemy.py for those, which
need a real Postgres session).
"""

import pytest

from app.models.hero import Hero
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.sqlalchemy import SQLAlchemyRepository


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
