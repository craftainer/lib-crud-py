"""Unit test: CompatCRUD's generic get/list/create/update/delete conversion logic.

Reuses tests/unit/crud/test_base.py's in-memory fake Repository and standalone
Pydantic view, wrapped in a small "legacy" view/converter pair, not tied to Hero at
all, to prove the wrapper is genuinely generic.
"""

import pytest
from pydantic import BaseModel, ConfigDict

from app.crud.base import CRUDInterface
from app.crud.compat import CompatCRUD
from tests.unit.crud.test_base import _FakeWidgetRepository, _Widget, _WidgetCreate, _WidgetUpdate


class _LegacyWidget(BaseModel):
    """Legacy view of a widget -- `tag` instead of `label`."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    tag: str


class _LegacyWidgetCreate(BaseModel):
    """Legacy view accepted when creating a widget."""

    tag: str


class _LegacyWidgetUpdate(BaseModel):
    """Legacy view accepted when updating a widget."""

    tag: str


def _to_legacy(widget: _Widget) -> _LegacyWidget:
    """Convert a current widget view down to the legacy shape."""
    return _LegacyWidget(id=widget.id, tag=widget.label)


def _from_legacy_create(payload: BaseModel) -> BaseModel:
    """Convert a legacy create payload up to the current shape."""
    assert isinstance(payload, _LegacyWidgetCreate)
    return _WidgetCreate(label=payload.tag)


def _from_legacy_update(payload: BaseModel) -> BaseModel:
    """Convert a legacy update payload up to the current shape."""
    assert isinstance(payload, _LegacyWidgetUpdate)
    return _WidgetUpdate(label=payload.tag)


@pytest.fixture
def compat_crud() -> CompatCRUD[_LegacyWidget, _Widget, object]:
    """Return a CompatCRUD wrapping a fresh CRUDInterface over an in-memory fake repository."""
    crud = CRUDInterface(schema=_Widget, repository=_FakeWidgetRepository())
    return CompatCRUD(
        crud,
        to_legacy=_to_legacy,
        from_legacy_create=_from_legacy_create,
        from_legacy_update=_from_legacy_update,
    )


async def test_create_and_get(compat_crud: CompatCRUD[_LegacyWidget, _Widget, object]) -> None:
    """create() converts up, persists, then converts the result back down."""
    created = await compat_crud.create(_LegacyWidgetCreate(tag="a"))
    assert created == _LegacyWidget(id=created.id, tag="a")
    assert await compat_crud.get(created.id) == created


async def test_get_missing_returns_none(
    compat_crud: CompatCRUD[_LegacyWidget, _Widget, object],
) -> None:
    """get() returns None for an id that doesn't exist."""
    assert await compat_crud.get(999) is None


async def test_list_returns_every_created_record_in_legacy_shape(
    compat_crud: CompatCRUD[_LegacyWidget, _Widget, object],
) -> None:
    """list() returns every created record, converted to the legacy shape."""
    await compat_crud.create(_LegacyWidgetCreate(tag="a"))
    await compat_crud.create(_LegacyWidgetCreate(tag="b"))
    assert [widget.tag for widget in await compat_crud.list()] == ["a", "b"]


async def test_update_converts_up_then_back_down(
    compat_crud: CompatCRUD[_LegacyWidget, _Widget, object],
) -> None:
    """update() converts the legacy payload up, persists it, and returns the legacy shape."""
    created = await compat_crud.create(_LegacyWidgetCreate(tag="a"))
    updated = await compat_crud.update(created.id, _LegacyWidgetUpdate(tag="b"))
    assert updated is not None
    assert updated.tag == "b"


async def test_update_missing_returns_none(
    compat_crud: CompatCRUD[_LegacyWidget, _Widget, object],
) -> None:
    """update() returns None for an id that doesn't exist."""
    assert await compat_crud.update(999, _LegacyWidgetUpdate(tag="b")) is None


async def test_delete(compat_crud: CompatCRUD[_LegacyWidget, _Widget, object]) -> None:
    """delete() removes the record and reports it existed; a second delete reports False."""
    created = await compat_crud.create(_LegacyWidgetCreate(tag="a"))
    assert await compat_crud.delete(created.id) is True
    assert await compat_crud.delete(created.id) is False
