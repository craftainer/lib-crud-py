"""Unit test: CRUDInterface's generic get/list/create/update/delete logic.

Uses an in-memory fake Repository and a small standalone Pydantic view, not tied
to Hero/SQLAlchemy at all, to prove the CRUD interface is genuinely generic.
"""

import asyncio
import ssl
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from crud.interfaces.base import (
    CRUDInterface,
    EventSink,
    InMemoryEventSink,
    MQTTEventSource,
    OwnerScope,
    _background_tasks,
    _mqtt_connection_kwargs,
)
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.stats import ResourceStats, TimeBucket


@dataclass
class _WidgetRecord:
    """Stand-in for a persisted record, independent of any ORM."""

    id: int
    label: str
    owner_id: str = ""


class _Widget(BaseModel):
    """View returned by the CRUD interface."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    owner_id: str = ""


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
        self.last_stats_filters: Sequence[FilterClause] = ()

    def _matching(self, filters: Sequence[FilterClause]) -> list[_WidgetRecord]:
        def matches(record: _WidgetRecord, clause: FilterClause) -> bool:
            value = getattr(record, clause.field)
            if clause.op is FilterOp.EQ:
                return bool(value == clause.value)
            if clause.op is FilterOp.ICONTAINS:
                return str(clause.value).casefold() in str(value).casefold()
            raise NotImplementedError(clause.op)

        return [r for r in self._records.values() if all(matches(r, c) for c in filters)]

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> _WidgetRecord | None:
        """Return the record with the given id, or None if it doesn't exist."""
        return self._records.get(record_id)

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> list[_WidgetRecord]:
        """Return up to `limit` matching records, skipping the first `skip`."""
        matching = self._matching(filters)
        if sort:
            for clause in reversed(sort):
                matching = sorted(
                    matching, key=lambda r: getattr(r, clause.field), reverse=clause.descending
                )
        return matching[skip : skip + limit]

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters."""
        return len(self._matching(filters))

    async def restore(self, record_id: int) -> _WidgetRecord | None:
        """Return the record with the given id, or None -- _WidgetRecord has no archived state.

        Standing in for crud.repositories.{memory,sqlalchemy}'s real Archivable-gated
        `restore`, which is exercised directly against Hero in tests/unit/repositories/
        test_memory.py and tests/integration/repositories/test_sqlalchemy.py -- this
        fake only needs to prove CRUDInterface.restore's own id-lookup/owner-scoping
        logic, not archived-column semantics.
        """
        return self._records.get(record_id)

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[_WidgetRecord]:
        """Return every record matching the filters -- see restore()'s own docstring above."""
        return self._matching(filters)

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

    async def stats(
        self,
        *,
        numeric_fields: Sequence[str],
        categorical_fields: Sequence[str],
        filters: Sequence[FilterClause] = (),
        bucket: TimeBucket | None = None,
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> ResourceStats:
        """Record the filters it was called with (via `self.last_stats_filters`) and
        return a minimal ResourceStats -- enough to prove CRUDInterface.stats is a
        thin, correctly-scoped pass-through, without duplicating the real
        aggregation logic crud.repositories.memory/crud.repositories.sqlalchemy own.
        """
        self.last_stats_filters = filters
        return ResourceStats(
            total=len(self._matching(filters)),
            numeric={},
            categorical={},
            time_series=None,
            lifecycle=None,
        )


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


async def test_stats_is_a_thin_pass_through(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """stats() forwards straight to the repository and returns its ResourceStats unchanged."""
    await crud.create(_WidgetCreate(label="a"))
    await crud.create(_WidgetCreate(label="b"))
    result = await crud.stats(numeric_fields=(), categorical_fields=())
    assert result.total == 2


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


async def test_restore(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """restore() returns the record as a view when the repository finds it."""
    created = await crud.create(_WidgetCreate(label="a"))
    restored = await crud.restore(created.id)
    assert restored is not None
    assert restored.id == created.id


async def test_restore_missing_returns_none(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """restore() returns None for an id the repository can't find."""
    assert await crud.restore(999) is None


async def test_restore_many(crud: CRUDInterface[_Widget, _WidgetRecord]) -> None:
    """restore_many() returns every record matching the filters, as views."""
    await crud.create(_WidgetCreate(label="apple"))
    await crud.create(_WidgetCreate(label="banana"))
    restored = await crud.restore_many(filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")])
    assert {widget.label for widget in restored} == {"apple"}


# --- OwnerScope: opt-in per-owner scoping ------------------------------------
#
# Two CRUDInterfaces sharing one repository, scoped to different owners, prove
# neither can reach the other's records -- mirroring how a real resource would
# build one CRUDInterface per request from the caller's own claims.


@pytest.fixture
def repository() -> _FakeWidgetRepository:
    """Return a fresh in-memory fake repository, shared by two owner-scoped interfaces."""
    return _FakeWidgetRepository()


@pytest.fixture
def alice_crud(repository: _FakeWidgetRepository) -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface scoped to owner "alice", backed by the shared repository."""
    return CRUDInterface(
        schema=_Widget, repository=repository, owner=OwnerScope("owner_id", "alice")
    )


@pytest.fixture
def bob_crud(repository: _FakeWidgetRepository) -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface scoped to owner "bob", backed by the shared repository."""
    return CRUDInterface(schema=_Widget, repository=repository, owner=OwnerScope("owner_id", "bob"))


async def test_owner_create_stamps_owner_field_ignoring_input(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """create() stamps the owner field from the scope, not from caller input."""
    created = await alice_crud.create(_WidgetCreate(label="a"))
    assert created.owner_id == "alice"


async def test_owner_get_cannot_reach_another_owners_record(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """get() returns None for a record that exists but belongs to a different owner."""
    alices = await alice_crud.create(_WidgetCreate(label="a"))
    assert await bob_crud.get(alices.id) is None
    assert await alice_crud.get(alices.id) == alices


async def test_owner_stats_only_counts_own_records(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
    repository: _FakeWidgetRepository,
) -> None:
    """stats() applies the owner's read-scoping filter (read_scoped=True, the default)."""
    await alice_crud.create(_WidgetCreate(label="apple"))
    await bob_crud.create(_WidgetCreate(label="banana"))
    result = await alice_crud.stats(numeric_fields=(), categorical_fields=())
    assert result.total == 1
    assert FilterClause("owner_id", FilterOp.EQ, "alice") in repository.last_stats_filters


async def test_owner_list_only_returns_own_records(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """list() only returns the records owned by this interface's scope, filters included."""
    await alice_crud.create(_WidgetCreate(label="apple"))
    await bob_crud.create(_WidgetCreate(label="banana"))
    assert [w.label for w in await alice_crud.list()] == ["apple"]
    assert [w.label for w in await bob_crud.list()] == ["banana"]


async def test_owner_count_only_counts_own_records(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """count() only counts records owned by this interface's scope."""
    await alice_crud.create(_WidgetCreate(label="apple"))
    await bob_crud.create(_WidgetCreate(label="banana"))
    assert await alice_crud.count() == 1
    assert await bob_crud.count() == 1


async def test_owner_update_cannot_reach_another_owners_record(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """update() returns None (and makes no change) for another owner's record."""
    alices = await alice_crud.create(_WidgetCreate(label="a"))
    assert await bob_crud.update(alices.id, _WidgetUpdate(label="hijacked")) is None
    assert (await alice_crud.get(alices.id)).label == "a"  # type: ignore[union-attr]


async def test_owner_delete_cannot_reach_another_owners_record(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """delete() reports False (and deletes nothing) for another owner's record."""
    alices = await alice_crud.create(_WidgetCreate(label="a"))
    assert await bob_crud.delete(alices.id) is False
    assert await alice_crud.get(alices.id) == alices


async def test_owner_restore_cannot_reach_another_owners_record(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """restore() returns None for another owner's record, and the record for one's own."""
    alices = await alice_crud.create(_WidgetCreate(label="a"))
    assert await bob_crud.restore(alices.id) is None
    restored = await alice_crud.restore(alices.id)
    assert restored is not None
    assert restored.id == alices.id


async def test_owner_update_many_only_matches_own_records(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """update_many() only applies to records owned by this interface's scope."""
    await alice_crud.create(_WidgetCreate(label="apple"))
    await bob_crud.create(_WidgetCreate(label="apricot"))
    updated = await bob_crud.update_many(
        filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")],
        data=_WidgetUpdate(label="updated"),
    )
    assert [w.label for w in updated] == ["updated"]
    assert (await alice_crud.get((await alice_crud.list())[0].id)).label == "apple"  # type: ignore[union-attr]


async def test_owner_delete_many_only_matches_own_records(
    alice_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """delete_many() only deletes records owned by this interface's scope."""
    await alice_crud.create(_WidgetCreate(label="apple"))
    await bob_crud.create(_WidgetCreate(label="apricot"))
    deleted = await bob_crud.delete_many(filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")])
    assert len(deleted) == 1
    assert await alice_crud.count() == 1
    assert await bob_crud.count() == 0


# --- OwnerScope(read_scoped=False): open reads, owner-restricted writes ------


@pytest.fixture
def alice_open_reads_crud(
    repository: _FakeWidgetRepository,
) -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface scoped to "alice" with reads opened to every owner."""
    return CRUDInterface(
        schema=_Widget,
        repository=repository,
        owner=OwnerScope("owner_id", "alice", read_scoped=False),
    )


@pytest.fixture
def bob_open_reads_crud(
    repository: _FakeWidgetRepository,
) -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface scoped to "bob" with reads opened to every owner."""
    return CRUDInterface(
        schema=_Widget,
        repository=repository,
        owner=OwnerScope("owner_id", "bob", read_scoped=False),
    )


async def test_read_scoped_false_lets_every_owner_list_every_record(
    alice_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """list()/count() see every record regardless of owner when read_scoped=False."""
    await alice_open_reads_crud.create(_WidgetCreate(label="apple"))
    await bob_open_reads_crud.create(_WidgetCreate(label="banana"))
    assert {w.label for w in await alice_open_reads_crud.list()} == {"apple", "banana"}
    assert await bob_open_reads_crud.count() == 2


async def test_read_scoped_false_lets_every_owner_get_by_id(
    alice_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """get() finds another owner's record by id when read_scoped=False."""
    alices = await alice_open_reads_crud.create(_WidgetCreate(label="apple"))
    found = await bob_open_reads_crud.get(alices.id)
    assert found == alices


async def test_read_scoped_false_still_blocks_update_of_another_owners_record(
    alice_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """update() still 404s (returns None) for another owner's record, reads aside."""
    alices = await alice_open_reads_crud.create(_WidgetCreate(label="apple"))
    assert await bob_open_reads_crud.update(alices.id, _WidgetUpdate(label="hijacked")) is None
    assert (await alice_open_reads_crud.get(alices.id)).label == "apple"  # type: ignore[union-attr]


async def test_read_scoped_false_still_blocks_delete_of_another_owners_record(
    alice_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
    bob_open_reads_crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """delete() still reports False for another owner's record, reads aside."""
    alices = await alice_open_reads_crud.create(_WidgetCreate(label="apple"))
    assert await bob_open_reads_crud.delete(alices.id) is False
    assert await alice_open_reads_crud.get(alices.id) == alices


# --- EventSink: opt-in real-time event publishing -----------------------------
#
# Mirrors the RevisionSink tests' shape (a fake recording every call), but
# EventSink is fired for restore/restore_many too, unlike RevisionSink -- see
# EventSink's own docstring for why.


class _FakeEventSink:
    """EventSink recording every publish() call, for assertions."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self, *, resource: str, record_id: int, action: str, snapshot: dict[str, Any]
    ) -> None:
        """Record this call's arguments."""
        self.calls.append(
            {"resource": resource, "record_id": record_id, "action": action, "snapshot": snapshot}
        )


@pytest.fixture
def event_sink() -> _FakeEventSink:
    """Return a fresh fake EventSink."""
    return _FakeEventSink()


@pytest.fixture
def event_crud(event_sink: EventSink) -> CRUDInterface[_Widget, _WidgetRecord]:
    """Return a CRUDInterface with `events` set, backed by a fresh in-memory repository."""
    return CRUDInterface(
        schema=_Widget, repository=_FakeWidgetRepository(), events=event_sink, resource="widget"
    )


async def test_events_fired_on_create(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """create() publishes one "create" event with the created record's snapshot."""
    created = await event_crud.create(_WidgetCreate(label="a"))
    assert event_sink.calls == [
        {
            "resource": "widget",
            "record_id": created.id,
            "action": "create",
            "snapshot": created.model_dump(mode="json"),
        }
    ]


async def test_events_fired_on_update(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """update() publishes one "update" event with the updated record's snapshot."""
    created = await event_crud.create(_WidgetCreate(label="a"))
    event_sink.calls.clear()
    updated = await event_crud.update(created.id, _WidgetUpdate(label="b"))
    assert updated is not None
    assert event_sink.calls == [
        {
            "resource": "widget",
            "record_id": created.id,
            "action": "update",
            "snapshot": updated.model_dump(mode="json"),
        }
    ]


async def test_events_not_fired_on_missing_update(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """update() publishes nothing for an id that doesn't exist."""
    assert await event_crud.update(999, _WidgetUpdate(label="b")) is None
    assert event_sink.calls == []


async def test_events_fired_on_delete(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """delete() publishes one "delete" event with the pre-delete snapshot."""
    created = await event_crud.create(_WidgetCreate(label="a"))
    event_sink.calls.clear()
    assert await event_crud.delete(created.id) is True
    assert event_sink.calls == [
        {
            "resource": "widget",
            "record_id": created.id,
            "action": "delete",
            "snapshot": created.model_dump(mode="json"),
        }
    ]


async def test_events_not_fired_on_missing_delete(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """delete() publishes nothing for an id that doesn't exist."""
    assert await event_crud.delete(999) is False
    assert event_sink.calls == []


async def test_events_fired_on_update_many(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """update_many() publishes one "update" event per matched record."""
    await event_crud.create(_WidgetCreate(label="apple"))
    await event_crud.create(_WidgetCreate(label="apricot"))
    event_sink.calls.clear()
    updated = await event_crud.update_many(
        filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")], data=_WidgetUpdate(label="new")
    )
    assert [call["action"] for call in event_sink.calls] == ["update", "update"]
    assert {call["record_id"] for call in event_sink.calls} == {w.id for w in updated}


async def test_events_fired_on_delete_many(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """delete_many() publishes one "delete" event per deleted record."""
    await event_crud.create(_WidgetCreate(label="apple"))
    await event_crud.create(_WidgetCreate(label="apricot"))
    event_sink.calls.clear()
    deleted = await event_crud.delete_many(
        filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")]
    )
    assert [call["action"] for call in event_sink.calls] == ["delete", "delete"]
    assert {call["record_id"] for call in event_sink.calls} == {w.id for w in deleted}


async def test_events_fired_on_restore(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """restore() publishes one "restore" event -- unlike RevisionSink, which never fires here."""
    created = await event_crud.create(_WidgetCreate(label="a"))
    event_sink.calls.clear()
    restored = await event_crud.restore(created.id)
    assert restored is not None
    assert event_sink.calls == [
        {
            "resource": "widget",
            "record_id": created.id,
            "action": "restore",
            "snapshot": restored.model_dump(mode="json"),
        }
    ]


async def test_events_not_fired_on_missing_restore(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """restore() publishes nothing for an id the repository can't find."""
    assert await event_crud.restore(999) is None
    assert event_sink.calls == []


async def test_events_fired_on_restore_many(
    event_crud: CRUDInterface[_Widget, _WidgetRecord], event_sink: _FakeEventSink
) -> None:
    """restore_many() publishes one "restore" event per restored record."""
    await event_crud.create(_WidgetCreate(label="apple"))
    await event_crud.create(_WidgetCreate(label="apricot"))
    event_sink.calls.clear()
    restored = await event_crud.restore_many(
        filters=[FilterClause("label", FilterOp.ICONTAINS, "ap")]
    )
    assert [call["action"] for call in event_sink.calls] == ["restore", "restore"]
    assert {call["record_id"] for call in event_sink.calls} == {w.id for w in restored}


async def test_no_events_published_when_events_not_configured(
    crud: CRUDInterface[_Widget, _WidgetRecord],
) -> None:
    """A CRUDInterface built with events=None (the default) never touches any sink."""
    created = await crud.create(_WidgetCreate(label="a"))
    assert await crud.update(created.id, _WidgetUpdate(label="b")) is not None
    assert await crud.restore(created.id) is not None
    assert await crud.delete(created.id) is True


# --- InMemoryEventSink: the MODE=mock EventSink/EventSource, tested directly --


async def test_in_memory_event_sink_delivers_to_a_subscribed_queue() -> None:
    """subscribe() then publish() delivers the event to that subscriber's iterator."""
    sink = InMemoryEventSink()
    subscriber_id, events = await sink.subscribe(None)
    assert subscriber_id  # a fresh id was issued

    await sink.publish(resource="hero", record_id=1, action="create", snapshot={"id": 1})

    event = await events.__anext__()
    assert event["resource"] == "hero"
    assert event["record_id"] == 1
    assert event["action"] == "create"
    assert event["snapshot"] == {"id": 1}
    assert "timestamp" in event


async def test_in_memory_event_sink_reuses_a_given_subscriber_id() -> None:
    """subscribe() with an explicit subscriber_id echoes it back rather than issuing a new one."""
    sink = InMemoryEventSink()
    subscriber_id, _ = await sink.subscribe("known-id")
    assert subscriber_id == "known-id"


async def test_in_memory_event_sink_fans_out_to_every_subscriber() -> None:
    """A single publish() reaches every currently-subscribed queue, not just the first."""
    sink = InMemoryEventSink()
    _, first_events = await sink.subscribe(None)
    _, second_events = await sink.subscribe(None)

    await sink.publish(resource="hero", record_id=1, action="create", snapshot={"id": 1})

    assert (await first_events.__anext__())["record_id"] == 1
    assert (await second_events.__anext__())["record_id"] == 1


async def test_in_memory_event_sink_publish_with_no_subscribers_is_a_no_op() -> None:
    """publish() with nothing subscribed yet doesn't raise."""
    sink: EventSink = InMemoryEventSink()
    await sink.publish(resource="hero", record_id=1, action="create", snapshot={"id": 1})


async def test_in_memory_event_sink_drops_queue_when_subscriber_stops_consuming() -> None:
    """A subscriber that stops iterating (SSE disconnect) has its queue removed.

    Without this, every subscribe()/disconnect cycle would leave a queue behind
    forever -- see InMemoryEventSink._events's own docstring.
    """
    sink = InMemoryEventSink()
    subscriber_id, events = await sink.subscribe(None)
    assert subscriber_id in sink._queues

    # Start the generator (an unstarted one has no frame for aclose() to run
    # `finally` against) without waiting forever on its empty queue.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(events.__anext__(), timeout=0.01)
    await cast("AsyncGenerator[dict[str, Any]]", events).aclose()

    assert subscriber_id not in sink._queues


def test_mqtt_connection_kwargs_without_tls_omits_tls_context() -> None:
    """No TLS: the returned kwargs carry only username/password, no tls_context."""
    kwargs = _mqtt_connection_kwargs(
        username="u",
        password="p",  # noqa: S106 -- test fixture value, not a real secret
        use_tls=False,
    )
    assert kwargs == {"username": "u", "password": "p"}


def test_mqtt_connection_kwargs_with_tls_adds_a_default_ssl_context() -> None:
    """TLS enabled: the returned kwargs also carry a default SSLContext."""
    kwargs = _mqtt_connection_kwargs(
        username="u",
        password="p",  # noqa: S106 -- test fixture value, not a real secret
        use_tls=True,
    )
    assert kwargs["username"] == "u"
    assert kwargs["password"] == "p"  # noqa: S105 -- test fixture value, not a real secret
    assert isinstance(kwargs["tls_context"], ssl.SSLContext)


class _FailingDisconnectMessages:
    """An empty async message iterator, standing in for aiomqtt.Client.messages."""

    def __aiter__(self) -> _FailingDisconnectMessages:
        return self

    async def __anext__(self) -> dict[str, Any]:
        raise StopAsyncIteration


class _FailingDisconnectClient:
    """Stand-in for aiomqtt.Client whose disconnect (__aexit__) always fails."""

    messages = _FailingDisconnectMessages()

    async def __aexit__(self, *args: object) -> None:
        raise RuntimeError("broker unreachable")


async def test_mqtt_event_source_logs_a_failed_disconnect(caplog: pytest.LogCaptureFixture) -> None:
    """A disconnect failure in MQTTEventSource._events's detached task is logged, not lost.

    See crud.interfaces.base._events's own docstring for why disconnecting is a
    detached background task rather than a plain `await` in this generator's own
    `finally` -- this asserts that detached task's own failure still leaves a trace.
    """
    source = MQTTEventSource(hostname="broker", port=1883, resource="hero", keepalive=60)
    events = source._events(_FailingDisconnectClient())  # type: ignore[arg-type]

    with caplog.at_level("DEBUG", logger="crud.interfaces.base"):
        assert [event async for event in events] == []
        pending = list(_background_tasks)
        await asyncio.gather(*pending)

    assert "MQTT disconnect failed" in caplog.text
