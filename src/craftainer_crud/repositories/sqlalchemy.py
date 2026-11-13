"""Generic SQLAlchemy implementation of the Repository protocol.

Parameterized purely by a SQLAlchemy model class (craftainer_crud.models.base.IdentifiedBase
subclass) -- adding a new resource never requires a new repository class, only a
new model.

A few FilterOp branches below are `# pragma: no cover`: craftainer_crud.controllers.crud_query
(the only place that turns an HTTP query string into a FilterClause) never emits
NE/LT/GT/CONTAINS -- its wire format only ever produces EQ/GTE/LTE/IN/ICONTAINS/
REGEX (see its module docstring) -- so those branches can never run through the
real HTTP stack tests/integration/tests/e2e exercise. tests/integration/
repositories/test_sqlalchemy.py's test_every_filter_op_against_real_postgres
calls this repository directly to exercise every FilterOp regardless; the pragma
only affects what's counted toward the e2e coverage gate specifically.

The trailing `case _` in `_where_clauses`'s match is `# pragma: no cover` for a
different reason: FilterOp is exhaustive over the cases above it, so the branch
is unreachable by construction -- it exists only so coverage.py doesn't count the
implicit "no case matched" fall-through of the match statement's last real case
as an untested branch.

Archivable/Schedulable/Lockable (see craftainer_crud.models.mixins) are detected via
`hasattr` on the bound model class -- a model without one of these mixins is
unaffected, matching how craftainer_crud.repositories.memory already special-cases
`created_at`/`updated_at`.

A few Archivable branches below are also `# pragma: no cover`, for the same
reason as craftainer_crud.repositories.memory's own module docstring: Hero is the only model
this app binds to this repository, and Hero always carries Archivable, so
delete()/delete_many()'s genuinely-hard-delete path and restore_many()'s
no-archived_at early return can never run through any test here without a second,
Archivable-less model existing purely to exercise them.

restore() (the non-`_many` single-record method) is also `# pragma: no cover`, for
the same reason as craftainer_crud.repositories.memory's own module docstring's second
reason: Hero's own CRUDInterface always sets `owner`, which routes a single-record
restore through this repository's own restore_many instead. tests/integration/
repositories/test_sqlalchemy.py calls restore() directly, which is what actually covers
it for the primary coverage gate. update()/delete() are not special any more: the
interface calls them for every single-record write, owner-scoped or not (via `scope`),
and the hard-delete branch is covered by a throwaway mixin-less model.

Optimistic concurrency: update()/delete() take `expected_version` (the record's
`updated_at`) and run one conditional statement. `updated_at` is set by the server's
`now()`, which Postgres evaluates once per *transaction*: two writes inside one
transaction produce the same version, and the version has the column's microsecond
granularity. See docs/adrs/0011-atomic-optimistic-concurrency.md.

`_lifecycle_stats`'s own `hasattr` checks are `# pragma: no cover` for the same
reason: Hero (the only model tests/e2e reaches this repository through) always
carries every one of Archivable/Draftable/Lockable/Schedulable, so each
check's "doesn't have this mixin" branch, and the `if not columns: return None`
below them, can never actually run through tests/e2e -- only through
tests/integration/repositories/test_sqlalchemy.py's own `_PlainRecord` (no
mixins at all), which is what covers them for the primary coverage gate.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    delete,
    func,
    literal_column,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from craftainer_crud.models.base import IdentifiedBase
from craftainer_crud.repositories.base import PreconditionFailedError, RecordLockedError
from craftainer_crud.repositories.filtering import FilterClause, FilterOp, SortClause
from craftainer_crud.repositories.stats import (
    LifecycleStats,
    NumericFieldStats,
    ResourceStats,
    TimeBucket,
    TimeBucketCount,
)


def _escape_like(value: object) -> str:
    """Escape `\\`, `%` and `_` so a user's text matches literally inside a LIKE pattern."""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_float(value: int | float | Decimal | None) -> float | None:
    """Coerce a SQL aggregate result (int/float/Decimal/None) to a plain float."""
    return None if value is None else float(value)


def _now() -> datetime:
    """Return the current time as naive UTC, matching how naive TIMESTAMP columns store it."""
    return datetime.now(UTC).replace(tzinfo=None)


def _mismatch(record_id: int, expected_version: datetime) -> str:
    """Return the PreconditionFailedError message for a record not at `expected_version`."""
    return f"record {record_id!r} is no longer at the expected version {expected_version!r}"


def _raise_if_locked(instance: object, data: dict[str, Any] | None) -> None:
    """Raise RecordLockedError unless `instance` isn't locked or `data` unlocks-only.

    See craftainer_crud.models.mixins.Lockable's own docstring: the unlock escape hatch only
    applies when `data` is exactly `{"is_locked": False}`, not a PATCH that also
    changes other fields in the same request.
    """
    if not getattr(instance, "is_locked", False):
        return
    if data is not None and data == {"is_locked": False}:
        return
    raise RecordLockedError(f"record {getattr(instance, 'id', '?')!r} is locked")


class SQLAlchemyRepository[ModelT: IdentifiedBase]:
    """Repository backed by a SQLAlchemy async session and a single mapped model."""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        """Bind this repository to a session and the SQLAlchemy model it persists."""
        self._session = session
        self._model = model
        self._column_names = frozenset(sa_inspect(model).columns.keys())

    @property
    def session(self) -> AsyncSession:
        """The session this repository writes through (for after-commit event delivery)."""
        return self._session

    async def _reload(self, instances: Sequence[ModelT]) -> None:
        """Re-read `instances` from the database in one SELECT, refreshing expired columns.

        A flushed UPDATE leaves `updated_at` (its `onupdate` is a SQL expression) expired,
        and an async session cannot lazy-load it, so the rows are re-read here. One
        `WHERE id IN (...)` with `populate_existing` replaces a per-row `session.refresh`
        (one SELECT per row) with one SELECT per batch; the identity map hands back the
        same objects, now carrying the server-generated values.
        """
        if not instances:
            return
        statement = (
            select(self._model)
            .where(self._model.id.in_([instance.id for instance in instances]))
            .execution_options(populate_existing=True)
        )
        await self._session.execute(statement)

    def _column(self, field: str) -> ColumnElement[Any]:
        """Return `field` as a mapped column of this repository's model, or raise.

        `FilterClause.field`/`SortClause.field` are only meant to reach here already
        validated against a resource's schema
        (craftainer_crud.controllers.crud_query.parse_filters/
        parse_sort) -- this check is defense-in-depth so a caller that bypasses that
        validation gets a clear error instead of either an AttributeError (an
        unrecognized name) or silently traversing a relationship attribute (a name
        that exists on the model but isn't a plain column).

        `# pragma: no cover` on the raise below is for tests/e2e specifically: it only
        ever reaches this repository through the real HTTP routes, which parse_filters/
        parse_sort already validate against Hero's schema, so an unknown field name can
        never actually arrive here that way. tests/unit/repositories/test_sqlalchemy.py's
        test_where_clauses_rejects_unknown_filter_field/test_order_by_rejects_unknown_
        sort_field call this repository directly, bypassing that validation, and still
        count toward that suite's own 95% gate.
        """
        if field not in self._column_names:
            raise ValueError(  # pragma: no cover -- see docstring above
                f"{field!r} is not a filterable/sortable column of {self._model!r}"
            )
        return getattr(self._model, field)  # type: ignore[no-any-return]

    def _where_clauses(self, filters: Sequence[FilterClause]) -> list[ColumnElement[bool]]:
        """Translate each FilterClause into a SQLAlchemy predicate on this model's columns."""
        clauses: list[ColumnElement[bool]] = []
        for clause in filters:
            column = self._column(clause.field)
            match clause.op:
                case FilterOp.EQ:
                    clauses.append(column == clause.value)
                case FilterOp.NE:  # pragma: no cover -- see module docstring
                    clauses.append(column != clause.value)
                case FilterOp.LT:  # pragma: no cover -- see module docstring
                    clauses.append(column < clause.value)
                case FilterOp.LTE:
                    clauses.append(column <= clause.value)
                case FilterOp.GT:  # pragma: no cover -- see module docstring
                    clauses.append(column > clause.value)
                case FilterOp.GTE:
                    clauses.append(column >= clause.value)
                case FilterOp.IN:
                    clauses.append(column.in_(clause.value))
                case FilterOp.CONTAINS:  # pragma: no cover -- see module docstring
                    clauses.append(column.contains(str(clause.value), autoescape=True))
                case FilterOp.ICONTAINS:
                    clauses.append(column.ilike(f"%{_escape_like(clause.value)}%", escape="\\"))
                case FilterOp.REGEX:
                    clauses.append(column.op("~")(clause.value))
                case _:  # pragma: no cover -- FilterOp is exhaustive above
                    raise AssertionError(clause.op)
        return clauses

    def _visibility_clauses(
        self, *, include_archived: bool, include_unpublished: bool, only_archived: bool = False
    ) -> list[ColumnElement[bool]]:
        """Extra WHERE clauses excluding archived/not-yet-or-no-longer-published rows.

        `only_archived` inverts the archived clause (keep just the archived rows) and
        wins over `include_archived`; like it, a no-op for a model without `archived_at`.

        Built with `datetime.now(UTC)` at query time (see craftainer_crud.models.mixins.
        Schedulable), never a stored boolean, so no background process needs to
        touch the row for it to stop/start being visible.
        """
        clauses: list[ColumnElement[bool]] = []
        if (only_archived or not include_archived) and hasattr(self._model, "archived_at"):
            archived_at = self._model.archived_at  # type: ignore[attr-defined]
            clauses.append(archived_at.is_not(None) if only_archived else archived_at.is_(None))
        if not include_unpublished and hasattr(self._model, "publish_at"):
            now = _now()
            publish_at = self._model.publish_at  # type: ignore[attr-defined]
            unpublish_at = self._model.unpublish_at  # type: ignore[attr-defined]
            clauses.append(or_(publish_at.is_(None), publish_at <= now))
            clauses.append(or_(unpublish_at.is_(None), unpublish_at > now))
        return clauses

    def _order_by(self, sort: Sequence[SortClause]) -> list[ColumnElement[Any]]:
        """Translate each SortClause into a SQLAlchemy ORDER BY term on this model's columns."""
        order: list[ColumnElement[Any]] = []
        for clause in sort:
            column = self._column(clause.field)
            order.append(column.desc() if clause.descending else column.asc())
        return order

    def _matching(self, filters: Sequence[FilterClause]) -> Select[Any]:
        # `select(Model)` is `Select[tuple[Model]]` on SQLAlchemy 2.0 but `Select[Model]` on
        # 2.1; `Any` is the one row type both versions accept, and every caller reads it
        # through `.scalars()`, which yields `ModelT` instances either way.
        return select(self._model).where(*self._where_clauses(filters))

    async def _fetch_all(self, statement: Select[Any]) -> Sequence[ModelT]:
        """Run a `_matching`-derived statement and return its model instances.

        The annotated local is what fixes the row type to `ModelT`: `statement` is
        `Select[Any]` (see `_matching`), so `.scalars().all()` is `Sequence[Any]`.
        """
        result = await self._session.execute(statement)
        instances: Sequence[ModelT] = result.scalars().all()
        return instances

    async def _guard_regex_timeout(self, filters: Sequence[FilterClause]) -> None:
        """Cap the current transaction's statement_timeout when `filters` includes a REGEX op.

        `column.op("~")` (see `_where_clauses`) hands an attacker-supplied pattern to
        Postgres's own regex engine verbatim -- craftainer_crud.controllers.crud_query's
        length cap
        bounds the pattern's size, not its worst-case backtracking cost, so a short
        pathological pattern (e.g. "(a+)+$") against a large/crafted column value can
        still run the query for a very long time. `SET LOCAL` only affects the current
        transaction (reset automatically on commit/rollback), so this never leaks into
        an unrelated query sharing the same pooled connection.
        """
        if any(clause.op is FilterOp.REGEX for clause in filters):
            await self._session.execute(text("SET LOCAL statement_timeout = '1s'"))

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> ModelT | None:
        """Return the record with the given id, or None if it doesn't exist."""
        instance = await self._session.get(self._model, record_id)
        if instance is None:
            return None
        if not include_archived and getattr(instance, "archived_at", None) is not None:
            return None
        if not include_unpublished:
            now = _now()
            publish_at = getattr(instance, "publish_at", None)
            unpublish_at = getattr(instance, "unpublish_at", None)
            if publish_at is not None and publish_at > now:
                return None
            if unpublish_at is not None and unpublish_at <= now:
                return None
        return instance

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
        """Return up to `limit` matching records, skipping the first `skip`."""
        await self._guard_regex_timeout(filters)
        order = self._order_by(sort) if sort else [self._model.id.asc()]
        statement = (
            self._matching(filters)
            .where(
                *self._visibility_clauses(
                    include_archived=include_archived,
                    include_unpublished=include_unpublished,
                    only_archived=only_archived,
                )
            )
            .order_by(*order)
            .offset(skip)
            .limit(limit)
        )
        return await self._fetch_all(statement)

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters.

        Called by craftainer_crud.controllers.crud_actions before a bulk update/delete, to cap
        how many records a single action can affect.
        """
        await self._guard_regex_timeout(filters)
        statement = (
            select(func.count())
            .select_from(self._model)
            .where(
                *self._where_clauses(filters),
                *self._visibility_clauses(
                    include_archived=include_archived,
                    include_unpublished=include_unpublished,
                    only_archived=only_archived,
                ),
            )
        )
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def create(self, data: dict[str, Any]) -> ModelT:
        """Insert a new row from the given field values and return it."""
        instance = self._model(**data)
        self._session.add(instance)
        # No reload: SQLAlchemy 2.0's default eager_defaults="auto" fetches the INSERT's
        # server-generated created_at/updated_at via RETURNING on asyncpg.
        await self._session.flush()
        return instance

    async def _writable(
        self,
        record_id: int,
        data: dict[str, Any] | None,
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause],
    ) -> tuple[ModelT | None, Sequence[ColumnElement[bool]]]:
        """Read the live, in-scope record fresh; return it with the write's WHERE terms.

        The read exists for the early outcomes (not found, version mismatch, locked); the
        write that follows repeats the version, scope, not-archived and not-locked
        predicates in its own WHERE, so a writer that slips in between is caught there, not
        here. It uses `populate_existing` so a stale identity-map copy is never trusted.
        """
        where = [
            self._model.id == record_id,
            *self._where_clauses(scope),
            *self._visibility_clauses(include_archived=False, include_unpublished=True),
        ]
        statement = select(self._model).where(*where).execution_options(populate_existing=True)
        instance = (await self._session.execute(statement)).scalars().first()
        if instance is None:
            return None, where
        if expected_version is not None:
            if instance.updated_at != expected_version:
                raise PreconditionFailedError(_mismatch(record_id, expected_version))
            where.append(self._model.updated_at == expected_version)
        _raise_if_locked(instance, data)
        if hasattr(self._model, "is_locked") and data != {"is_locked": False}:
            where.append(self._model.is_locked.is_not(True))  # type: ignore[attr-defined]
        return instance, where

    async def _explain_lost_write(
        self,
        record_id: int,
        data: dict[str, Any] | None,
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause],
    ) -> None:
        """Raise why a write that passed `_writable` then matched no row; return if it's gone.

        Another writer got in between the read and the write. Reading again says how: the
        record is gone (return: the caller reports not found), its version moved
        (PreconditionFailedError) or it got locked (RecordLockedError). If it reads as
        writable again (the other change was undone), report a conflict rather than
        pretending the record is missing.
        """
        instance, _ = await self._writable(
            record_id, data, expected_version=expected_version, scope=scope
        )
        if instance is not None:
            raise PreconditionFailedError(f"record {record_id!r} changed during the write")

    async def update(
        self,
        record_id: int,
        data: dict[str, Any],
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause] = (),
    ) -> ModelT | None:
        """Apply the given field values to the record with the given id, if it exists.

        One `UPDATE ... WHERE id AND scope AND updated_at = :expected RETURNING <row>`:
        the version check and the write are the same statement, so of two writers holding
        the same `expected_version` exactly one matches a row. The new `updated_at` is
        set explicitly (so even an empty `data` moves the version) to the later of the
        transaction's `now()` and the old value plus a microsecond: Postgres's `now()` is
        the transaction's start time and could repeat or run behind the stored value.
        """
        instance, where = await self._writable(
            record_id, data, expected_version=expected_version, scope=scope
        )
        if instance is None:
            return None
        statement = (
            update(self._model)
            .where(*where)
            .values({**data, "updated_at": self._next_version()})
            .returning(self._model)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        updated = (await self._session.execute(statement)).scalars().first()
        if updated is None:
            await self._explain_lost_write(
                record_id, data, expected_version=expected_version, scope=scope
            )
        return updated

    async def delete(
        self,
        record_id: int,
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause] = (),
    ) -> bool:
        """Delete the record with the given id; return whether it existed.

        Same single conditional statement as `update`: a soft delete is
        `UPDATE ... SET archived_at`, a hard delete is `DELETE ... WHERE`.
        """
        instance, where = await self._writable(
            record_id, None, expected_version=expected_version, scope=scope
        )
        if instance is None:
            return False
        if hasattr(instance, "archived_at"):
            soft = (
                update(self._model)
                .where(*where)
                .values(archived_at=_now(), updated_at=self._next_version())
                .returning(self._model)
                .execution_options(populate_existing=True, synchronize_session=False)
            )
            removed = (await self._session.execute(soft)).scalars().first() is not None
        else:
            hard = (
                delete(self._model)
                .where(*where)
                .returning(self._model.id)
                .execution_options(synchronize_session=False)
            )
            removed = (await self._session.execute(hard)).first() is not None
            if removed:
                self._session.expunge(instance)
        if not removed:
            await self._explain_lost_write(
                record_id, None, expected_version=expected_version, scope=scope
            )
            return False
        return True

    def _next_version(self) -> ColumnElement[datetime]:
        """Return the SQL for a write's new `updated_at`, strictly after the stored one."""
        return func.greatest(
            func.now(), self._model.updated_at + literal_column("interval '1 microsecond'")
        )

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: dict[str, Any]
    ) -> Sequence[ModelT]:
        """Apply the given field values to every record matching the filters; return them.

        Excludes archived rows by default, same as delete/get/list/count -- an
        archived record needs restore() before it can be touched again. Unlike
        those, a not-yet-or-no-longer-published (Schedulable) row is still
        reachable here -- it isn't deleted, just not currently visible, and an
        editor must still be able to correct a scheduled record before it goes
        live (see craftainer_crud.models.mixins.Schedulable).
        """
        await self._guard_regex_timeout(filters)
        statement = self._matching(filters).where(
            *self._visibility_clauses(include_archived=False, include_unpublished=True)
        )
        instances = await self._fetch_all(statement)
        for instance in instances:
            _raise_if_locked(instance, data)
        for instance in instances:
            for field, value in data.items():
                setattr(instance, field, value)
        await self._session.flush()
        await self._reload(instances)
        return instances

    async def delete_many(
        self, *, filters: Sequence[FilterClause], include_unpublished: bool = True
    ) -> Sequence[ModelT]:
        """Delete every record matching the filters; return the records that were deleted.

        Excludes already-archived rows, same as update_many above. A
        not-yet-or-no-longer-published row is still reachable by default (see
        update_many's own docstring for why); `include_unpublished=False` leaves it alone.
        """
        await self._guard_regex_timeout(filters)
        statement = self._matching(filters).where(
            *self._visibility_clauses(
                include_archived=False, include_unpublished=include_unpublished
            )
        )
        instances = await self._fetch_all(statement)
        for instance in instances:
            _raise_if_locked(instance, None)
        now = _now()
        for instance in instances:
            if hasattr(instance, "archived_at"):
                instance.archived_at = now
            else:
                await self._session.delete(instance)  # pragma: no cover -- see module docstring
        await self._session.flush()
        if hasattr(self._model, "archived_at"):  # pragma: no branch -- see module docstring
            await self._reload(instances)
        return instances

    async def restore(  # pragma: no cover -- see module docstring
        self, record_id: int
    ) -> ModelT | None:
        """Clear `archived_at` on the record with the given id; return it, or None."""
        instance = await self._session.get(self._model, record_id)
        if instance is None or not hasattr(instance, "archived_at"):
            return None
        instance.archived_at = None
        await self._session.flush()
        await self._reload([instance])
        return instance

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[ModelT]:
        """Clear `archived_at` on every record matching the filters; return them."""
        if not hasattr(self._model, "archived_at"):
            return []  # pragma: no cover -- see module docstring
        instances = await self._fetch_all(self._matching(filters))
        for instance in instances:
            instance.archived_at = None  # type: ignore[attr-defined]
        await self._session.flush()
        await self._reload(instances)
        return instances

    async def _numeric_stats(
        self, fields: Sequence[str], where: Sequence[ColumnElement[bool]]
    ) -> dict[str, NumericFieldStats]:
        """One query computing count/min/max/avg/sum for every numeric field at once."""
        if not fields:
            return {}
        columns = []
        for field in fields:
            column = self._column(field)
            columns.extend(
                [
                    func.count(column),
                    func.min(column),
                    func.max(column),
                    func.avg(column),
                    func.sum(column),
                ]
            )
        statement = select(*columns).select_from(self._model).where(*where)
        result = await self._session.execute(statement)
        row = result.one()
        stats: dict[str, NumericFieldStats] = {}
        for index, field in enumerate(fields):
            count, minimum, maximum, average, total = row[index * 5 : index * 5 + 5]
            stats[field] = NumericFieldStats(
                field=field,
                count=count,
                minimum=_as_float(minimum),
                maximum=_as_float(maximum),
                average=_as_float(average),
                total=_as_float(total),
            )
        return stats

    async def _categorical_stats(
        self, fields: Sequence[str], where: Sequence[ColumnElement[bool]]
    ) -> dict[str, dict[str, int]]:
        """One GROUP BY query per categorical field, giving that field's value distribution."""
        stats: dict[str, dict[str, int]] = {}
        for field in fields:
            column = self._column(field)
            statement = (
                select(column, func.count()).select_from(self._model).where(*where).group_by(column)
            )
            result = await self._session.execute(statement)
            stats[field] = {str(value): count for value, count in result.all()}
        return stats

    async def _time_series(
        self, bucket: TimeBucket, where: Sequence[ColumnElement[bool]]
    ) -> Sequence[TimeBucketCount]:
        """One GROUP BY date_trunc(bucket, created_at) query, ordered oldest-first."""
        created_at = self._model.created_at
        bucket_start = func.date_trunc(bucket.value, created_at)
        statement = (
            select(bucket_start, func.count())
            .select_from(self._model)
            .where(*where)
            .group_by(bucket_start)
            .order_by(bucket_start)
        )
        result = await self._session.execute(statement)
        return [
            TimeBucketCount(bucket_start=start.isoformat(), count=count)
            for start, count in result.all()
        ]

    async def _lifecycle_stats(self, where: Sequence[ColumnElement[bool]]) -> LifecycleStats | None:
        """One query with a COUNT(...) FILTER(WHERE ...) per record-lifecycle mixin present."""
        columns: dict[str, ColumnElement[Any]] = {}
        if hasattr(self._model, "archived_at"):  # pragma: no cover -- see module docstring
            archived_at = self._model.archived_at  # type: ignore[attr-defined]
            columns["archived"] = func.count().filter(archived_at.is_not(None))
        if hasattr(self._model, "is_draft"):  # pragma: no cover -- see module docstring
            is_draft = self._model.is_draft  # type: ignore[attr-defined]
            columns["draft"] = func.count().filter(is_draft.is_(True))
        if hasattr(self._model, "is_locked"):  # pragma: no cover -- see module docstring
            is_locked = self._model.is_locked  # type: ignore[attr-defined]
            columns["locked"] = func.count().filter(is_locked.is_(True))
        if hasattr(self._model, "publish_at"):  # pragma: no cover -- see module docstring
            now = _now()
            publish_at = self._model.publish_at  # type: ignore[attr-defined]
            unpublish_at = self._model.unpublish_at  # type: ignore[attr-defined]
            columns["scheduled_pending"] = func.count().filter(publish_at > now)
            columns["scheduled_expired"] = func.count().filter(
                unpublish_at.is_not(None), unpublish_at <= now
            )
        if not columns:  # pragma: no cover -- see module docstring
            return None
        statement = select(*columns.values()).select_from(self._model).where(*where)
        result = await self._session.execute(statement)
        row = result.one()
        values = dict(zip(columns.keys(), row, strict=True))
        return LifecycleStats(**values)

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
        """Return aggregate statistics for records matching the given filters."""
        await self._guard_regex_timeout(filters)
        where = [
            *self._where_clauses(filters),
            *self._visibility_clauses(
                include_archived=include_archived, include_unpublished=include_unpublished
            ),
        ]
        total_statement = select(func.count()).select_from(self._model).where(*where)
        total = (await self._session.execute(total_statement)).scalar_one()
        numeric = await self._numeric_stats(numeric_fields, where)
        categorical = await self._categorical_stats(categorical_fields, where)
        time_series = await self._time_series(bucket, where) if bucket is not None else None
        lifecycle = await self._lifecycle_stats(where)
        return ResourceStats(
            total=total,
            numeric=numeric,
            categorical=categorical,
            time_series=time_series,
            lifecycle=lifecycle,
        )
