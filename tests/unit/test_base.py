"""Unit test: CRUDInterface's generic get/list/create/update/delete logic.

Uses an in-memory fake Repository and a small standalone Pydantic view, not tied
to Hero/SQLAlchemy at all, to prove the CRUD interface is genuinely generic.
"""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.crud.base import CRUDInterface


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

    async def get(self, record_id: int) -> _WidgetRecord | None:
        """Return the record with the given id, or None if it doesn't exist."""
        return self._records.get(record_id)

    async def list(self, *, skip: int = 0, limit: int = 100) -> list[_WidgetRecord]:
        """Return up to `limit` records, skipping the first `skip`."""
        return list(self._records.values())[skip : skip + limit]

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
