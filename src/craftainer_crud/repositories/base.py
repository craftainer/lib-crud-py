"""Generic repository protocol: the storage-agnostic contract the CRUD interfaces use."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from craftainer_crud.repositories.filtering import FilterClause, SortClause
from craftainer_crud.repositories.stats import ResourceStats, TimeBucket


class RecordLockedError(Exception):
    """Raised by update/update_many/delete/delete_many against a Lockable record.

    Raised when the existing row's `is_locked` is True and the mutation isn't the
    one explicit escape hatch: an `update`/`update_many` call whose own `data` sets
    `is_locked=False`. craftainer_crud.controllers.crud_actions catches this and raises
    HTTPException(423) -- see craftainer_crud.models.mixins.Lockable.
    """


class PreconditionFailedError(Exception):
    """Raised by update/delete when the stored version isn't the caller's `expected_version`.

    The version is the record's `updated_at`. craftainer_crud.controllers.crud_actions
    catches this and raises HTTPException(412), the same response a stale `If-Match`
    gets -- see docs/adrs/0011-atomic-optimistic-concurrency.md.
    """


class Repository[ModelT](Protocol):
    """Storage-agnostic CRUD contract for a single kind of persisted record."""

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> ModelT | None:
        """Return the record with the given id, or None if it doesn't exist.

        For a model carrying craftainer_crud.models.mixins.Archivable/Schedulable, an archived
        or not-yet-/no-longer-published record is treated as not found unless the
        matching `include_*` flag is set. `list`/`count` also accept `only_archived`
        (default `False`): when `True` they keep just the archived rows, whatever
        `include_archived` says; a model without `archived_at` ignores it, like
        `include_archived`. `get` has no such flag.
        """
        ...

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> Sequence[ModelT]:
        """Return up to `limit` matching records, skipping the first `skip`.

        See `get`'s docstring for `include_archived`/`include_unpublished`.
        """
        ...

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters.

        See `get`'s docstring for `include_archived`/`include_unpublished`.
        """
        ...

    async def create(self, data: dict[str, Any]) -> ModelT:
        """Create and return a new record from the given field values."""
        ...

    async def update(
        self,
        record_id: int,
        data: dict[str, Any],
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause] = (),
    ) -> ModelT | None:
        """Update the record with the given id and return it, or None if it doesn't exist.

        `expected_version` is required: the record's `updated_at` the caller last saw, or
        None to opt out and write unconditionally. A value makes the write conditional,
        checked atomically with the write itself (one `UPDATE ... WHERE id = :id AND
        updated_at = :expected`, never a read followed by a write): a record whose
        stored version differs raises PreconditionFailedError and is left untouched.
        `scope` is extra predicates the record must satisfy (an owner restriction, say):
        a record outside it counts as not found, so None.

        Raises RecordLockedError for a Lockable record whose `is_locked` is True,
        unless `data` itself sets `is_locked=False` -- see RecordLockedError.
        """
        ...

    async def delete(
        self,
        record_id: int,
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause] = (),
    ) -> bool:
        """Delete the record with the given id; return whether it existed.

        `expected_version` and `scope` behave as in `update`; a version mismatch raises
        PreconditionFailedError and deletes nothing.

        For a model carrying craftainer_crud.models.mixins.Archivable, this sets `archived_at`
        instead of issuing a real delete. Raises RecordLockedError for a Lockable
        record whose `is_locked` is True.
        """
        ...

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: dict[str, Any]
    ) -> Sequence[ModelT]:
        """Apply the given field values to every record matching the filters; return them.

        Raises RecordLockedError (see `delete`'s docstring) for any matched record
        that's locked, unless `data` itself sets `is_locked=False`.
        """
        ...

    async def delete_many(
        self, *, filters: Sequence[FilterClause], include_unpublished: bool = True
    ) -> Sequence[ModelT]:
        """Delete every record matching the filters; return the records that were deleted.

        See `delete`'s docstring for the Archivable/Lockable behavior. Unlike `list`,
        `include_unpublished` defaults to True, so a not-yet-or-no-longer-published
        (Schedulable) row is deleted unless the caller passes False.
        """
        ...

    async def restore(self, record_id: int) -> ModelT | None:
        """Clear `archived_at` on the record with the given id; return it, or None.

        None both when the record doesn't exist and when the model isn't
        Archivable at all -- there's nothing to restore either way.
        """
        ...

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[ModelT]:
        """Clear `archived_at` on every record matching the filters; return them."""
        ...

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
        """Return aggregate statistics for records matching the given filters.

        `numeric_fields`/`categorical_fields` name which columns to compute
        min/max/avg/sum and value-distribution counts for, respectively (see
        craftainer_crud.controllers.crud_stats for how these are derived from a resource's
        schema). `bucket`, if given, additionally computes a time-bucketed count
        series over `created_at`. A model carrying one of craftainer_crud.models.mixins'
        record-lifecycle mixins (detected via `hasattr`, same as `list`/`get`/
        `count`) also gets a lifecycle breakdown; a model with none of them gets
        `lifecycle=None`. See `get`'s docstring for `include_archived`/
        `include_unpublished`.
        """
        ...
