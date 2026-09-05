"""Unit test: CRUDInterface's generic get/list/create/update/delete logic.

Uses an in-memory fake Repository and a small standalone Pydantic view, not tied
to Hero/SQLAlchemy at all, to prove the CRUD interface is genuinely generic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.crud.base import CRUDInterface
from app.repositories.filtering import FilterClause, FilterOp, SortClause


@dataclass
class _WidgetRecord:
    """Stand-in for a persisted record, independent of any ORM."""

    id: int
    label: str


class _Widget(BaseModel):
    """View returned by the CRUD interface."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str


class _WidgetCreate(BaseModel):
    """View accepted when creating a widget."""

    label: str


class _WidgetUpdate(BaseModel):
    """View accepted when updating a widget."""

    label: str


class _FakeWidgetRepository:
    """In-memory Repository implementation, keyed by id."""

    def __init__(self) -> None:
        """Start with no records and the first id to hand out."""
        self._records: dict[int, _WidgetRecord] = {}
        self._next_id = 1

    def _matching(self, filters: Sequence[FilterClause]) -> list[_WidgetRecord]:
        def matches(record: _WidgetRecord, clause: FilterClause) -> bool:
            value = getattr(record, clause.field)
            if clause.op is FilterOp.EQ:
                return bool(value == clause.value)
            if clause.op is FilterOp.ICONTAINS:
                return str(clause.value).casefold() in str(value).casefold()
            raise NotImplementedError(clause.op)

        return [r for r in self._records.values() if all(matches(r, c) for c in filters)]

    async def get(self, record_id: int) -> _WidgetRecord | None:
        """Return the record with the given id, or None if it doesn't exist."""
        return self._records.get(record_id)

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
    ) -> list[_WidgetRecord]:
        """Return up to `limit` matching records, skipping the first `skip`."""
        matching = self._matching(filters)
        if sort:
            for clause in reversed(sort):
                matching = sorted(
                    matching, key=lambda r: getattr(r, clause.field), reverse=clause.descending
                )
        return matching[skip : skip + limit]

    async def count(self, *, filters: Sequence[FilterClause] = ()) -> int:
        """Return how many records match the given filters."""
        return len(self._matching(filters))

    async def create(self, data: dict[str, Any]) -> _WidgetRecord:
        """Create and return a new record from the given field values."""
        record = _WidgetRecord(id=self._next_id, **data)
        self._records[self._next_id] = record
        self._next_id += 1
        return record

    async def update(self, record_id: int, data: dict[str, Any]) -> _WidgetRecord | None:
        """Update the record with the given id and return it, or None if it doesn't exist."""
        record = self._records.get(record_id)
        if record is None:
            return None
        for field, value in data.items():
            setattr(record, field, value)
        return record

    async def delete(self, record_id: int) -> bool:
        """Delete the record with the given id; return whether it existed."""
        return self._records.pop(record_id, None) is not None

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: dict[str, Any]
    ) -> Sequence[_WidgetRecord]:
        """Apply the given field values to every record matching the filters; return them."""
        matching = self._matching(filters)
        for record in matching:
            for field, value in data.items():
                setattr(record, field, value)
        return matching

    async def delete_many(self, *, filters: Sequence[FilterClause]) -> Sequence[_WidgetRecord]:
        """Delete every record matching the filters; return the records that were deleted."""
        matching = self._matching(filters)
        for record in matching:
            del self._records[record.id]
        return matching


@pytest.fixture
def crud() -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface bound to a fresh in-memory fake repository."""
    return CRUDInterface(schema=_Widget, repository=_FakeWidgetRepository())


async def test_create_and_get(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """create() persists a record and returns it as a view; get() finds it by id."""
    created = await crud.create(_WidgetCreate(label="a"))
    assert created == _Widget(id=created.id, label="a")
    assert await crud.get(created.id) == created


async def test_get_missing_returns_none(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """get() returns None for an id that doesn't exist."""
    assert await crud.get(999) is None


async def test_list_returns_every_created_record(
    crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """list() returns every created record as a view."""
    await crud.create(_WidgetCreate(label="a"))
    await crud.create(_WidgetCreate(label="b"))
    assert [widget.label for widget in await crud.list()] == ["a", "b"]


async def test_list_applies_filters(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """list() passes filters through to the repository."""
    await crud.create(_WidgetCreate(label="apple"))
    await crud.create(_WidgetCreate(label="banana"))
    filtered = await crud.list(filters=[FilterClause("label", FilterOp.EQ, "apple")])
    assert [widget.label for widget in filtered] == ["apple"]


async def test_list_applies_sort(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """list() passes sort clauses through to the repository."""
    await crud.create(_WidgetCreate(label="b"))
    await crud.create(_WidgetCreate(label="a"))
    sorted_widgets = await crud.list(sort=[SortClause("label")])
    assert [widget.label for widget in sorted_widgets] == ["a", "b"]


async def test_count_matches_filters(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """count() reports how many records match the given filters."""
    await crud.create(_WidgetCreate(label="a"))
    await crud.create(_WidgetCreate(label="b"))
    assert await crud.count() == 2
    assert await crud.count(filters=[FilterClause("label", FilterOp.EQ, "a")]) == 1


async def test_update_applies_only_set_fields(
    crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """update() persists the new value and returns the updated view."""
    created = await crud.create(_WidgetCreate(label="a"))
    updated = await crud.update(created.id, _WidgetUpdate(label="b"))
    assert updated is not None
    assert updated.label == "b"


async def test_update_missing_returns_none(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """update() returns None for an id that doesn't exist."""
    assert await crud.update(999, _WidgetUpdate(label="b")) is None


async def test_delete(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """delete() removes the record and reports it existed; a second delete reports False."""
    created = await crud.create(_WidgetCreate(label="a"))
    assert await crud.delete(created.id) is True
    assert await crud.delete(created.id) is False


async def test_update_many_applies_to_every_match(
    crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """update_many() applies the update to every record matching the filters."""
    await crud.create(_WidgetCreate(label="apple"))
    await crud.create(_WidgetCreate(label="apricot"))
    await crud.create(_WidgetCreate(label="banana"))
    updated = await crud.update_many(
        filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")],
        data=_WidgetUpdate(label="updated"),
    )
    assert {widget.label for widget in updated} == {"updated"}
    assert len(updated) == 2
    assert await crud.count() == 3


async def test_delete_many_removes_every_match(
    crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """delete_many() removes every record matching the filters and returns them."""
    await crud.create(_WidgetCreate(label="apple"))
    await crud.create(_WidgetCreate(label="apricot"))
    await crud.create(_WidgetCreate(label="banana"))
    deleted = await crud.delete_many(filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")])
    assert len(deleted) == 2
    assert await crud.count() == 1
