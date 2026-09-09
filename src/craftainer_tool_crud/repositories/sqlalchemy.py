"""Generic SQLAlchemy implementation of the Repository protocol.

Parameterized purely by a SQLAlchemy model class (crud.models.base.IdentifiedBase
subclass) -- adding a new resource never requires a new repository class, only a
new model.

A few FilterOp branches below are `# pragma: no cover`: crud.controllers.crud_query
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

Archivable/Schedulable/Lockable (see crud.models.mixins) are detected via
`hasattr` on the bound model class -- a model without one of these mixins is
unaffected, matching how crud.repositories.memory already special-cases
`created_at`/`updated_at`.

A few Archivable branches below are also `# pragma: no cover`, for the same
reason as crud.repositories.memory's own module docstring: Hero is the only model
this app binds to this repository, and Hero always carries Archivable, so
delete()/delete_many()'s genuinely-hard-delete path and restore_many()'s
no-archived_at early return can never run through any test here without a second,
Archivable-less model existing purely to exercise them.

update()/delete()/restore() (the non-`_many` single-record methods) are also
`# pragma: no cover`, for the same reason as crud.repositories.memory's own
module docstring's second reason: Hero's own CRUDInterface always sets `owner`
(see app.crud_1.heroes.heroes_v2.get_hero_crud), which routes every
single-record update/delete/restore through this repository's own
update_many/delete_many/restore_many instead (see crud.interfaces.base.
OwnerScope's docstring) -- so those three methods can never run through
tests/e2e. tests/integration/repositories/test_sqlalchemy.py calls them
directly, which is what actually covers them for the primary coverage gate;
the pragma only affects what's counted toward the separate `pytest tests/e2e`
coverage gate.

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

from sqlalchemy import ColumnElement, Select, func, or_, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from crud.models.base import IdentifiedBase
from crud.repositories.base import RecordLockedError
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.stats import (
    LifecycleStats,
    NumericFieldStats,
    ResourceStats,
    TimeBucket,
    TimeBucketCount,
)


def _as_float(value: int | float | Decimal | None) -> float | None:
    """Coerce a SQL aggregate result (int/float/Decimal/None) to a plain float."""
    return None if value is None else float(value)


def _now() -> datetime:
    """Return the current time as naive UTC, matching how naive TIMESTAMP columns store it."""
    return datetime.now(UTC).replace(tzinfo=None)


def _raise_if_locked(instance: object, data: dict[str, Any] | None) -> None:
    """Raise RecordLockedError unless `instance` isn't locked or `data` unlocks-only.

    See crud.models.mixins.Lockable's own docstring: the unlock escape hatch only
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

    def _column(self, field: str) -> ColumnElement[Any]:
        """Return `field` as a mapped column of this repository's model, or raise.

        `FilterClause.field`/`SortClause.field` are only meant to reach here already
        validated against a resource's schema (crud.controllers.crud_query.parse_filters/
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
        if field not in sa_inspect(self._model).columns.keys():  # noqa: SIM118 -- .keys() is a ColumnCollection, not a dict
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
                    clauses.append(column.contains(clause.value))
                case FilterOp.ICONTAINS:
                    clauses.append(column.ilike(f"%{clause.value}%"))
                case FilterOp.REGEX:
                    clauses.append(column.op("~")(clause.value))
                case _:  # pragma: no cover -- FilterOp is exhaustive above
                    raise AssertionError(clause.op)
        return clauses

    def _visibility_clauses(
        self, *, include_archived: bool, include_unpublished: bool
    ) -> list[ColumnElement[bool]]:
        """Extra WHERE clauses excluding archived/not-yet-or-no-longer-published rows.

        Built with `datetime.now(UTC)` at query time (see crud.models.mixins.
        Schedulable), never a stored boolean, so no background process needs to
        touch the row for it to stop/start being visible.
        """
        clauses: list[ColumnElement[bool]] = []
        if not include_archived and hasattr(self._model, "archived_at"):
            archived_at = self._model.archived_at  # type: ignore[attr-defined]
            clauses.append(archived_at.is_(None))
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

    def _matching(self, filters: Sequence[FilterClause]) -> Select[tuple[ModelT]]:
        return select(self._model).where(*self._where_clauses(filters))

    async def _guard_regex_timeout(self, filters: Sequence[FilterClause]) -> None:
        """Cap the current transaction's statement_timeout when `filters` includes a REGEX op.

        `column.op("~")` (see `_where_clauses`) hands an attacker-supplied pattern to
        Postgres's own regex engine verbatim -- crud.controllers.crud_query's length cap
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
        include_unpublished: bool = False,
    ) -> Sequence[ModelT]:
        """Return up to `limit` matching records, skipping the first `skip`."""
        await self._guard_regex_timeout(filters)
        order = self._order_by(sort) if sort else [self._model.id.asc()]
        statement = (
            self._matching(filters)
            .where(
                *self._visibility_clauses(
                    include_archived=include_archived, include_unpublished=include_unpublished
                )
            )
            .order_by(*order)
            .offset(skip)
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return result.scalars().all()

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters.

        Called by crud.controllers.crud_actions before a bulk update/delete, to cap
        how many records a single action can affect.
        """
        await self._guard_regex_timeout(filters)
        statement = (
            select(func.count())
            .select_from(self._model)
            .where(
                *self._where_clauses(filters),
                *self._visibility_clauses(
                    include_archived=include_archived, include_unpublished=include_unpublished
                ),
            )
        )
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def create(self, data: dict[str, Any]) -> ModelT:
        """Insert a new row from the given field values and return it."""
        instance = self._model(**data)
        self._session.add(instance)
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def update(  # pragma: no cover -- see module docstring
        self, record_id: int, data: dict[str, Any]
    ) -> ModelT | None:
        """Apply the given field values to the record with the given id, if it exists."""
        instance = await self._session.get(self._model, record_id)
        if instance is None or getattr(instance, "archived_at", None) is not None:
            return None
        _raise_if_locked(instance, data)
        for field, value in data.items():
            setattr(instance, field, value)
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def delete(self, record_id: int) -> bool:  # pragma: no cover -- see module docstring
        """Delete the record with the given id; return whether it existed."""
        instance = await self._session.get(self._model, record_id)
        if instance is None:
            return False
        if hasattr(instance, "archived_at"):
            if instance.archived_at is not None:
                return False
            _raise_if_locked(instance, None)
            instance.archived_at = _now()
            await self._session.flush()
            return True
        _raise_if_locked(instance, None)
        await self._session.delete(instance)
        await self._session.flush()
        return True

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: dict[str, Any]
    ) -> Sequence[ModelT]:
        """Apply the given field values to every record matching the filters; return them.

        Excludes archived rows by default, same as delete/get/list/count -- an
        archived record needs restore() before it can be touched again. Unlike
        those, a not-yet-or-no-longer-published (Schedulable) row is still
        reachable here -- it isn't deleted, just not currently visible, and an
        editor must still be able to correct a scheduled record before it goes
        live (see crud.models.mixins.Schedulable).
        """
        await self._guard_regex_timeout(filters)
        statement = self._matching(filters).where(
            *self._visibility_clauses(include_archived=False, include_unpublished=True)
        )
        result = await self._session.execute(statement)
        instances = result.scalars().all()
        for instance in instances:
            _raise_if_locked(instance, data)
        for instance in instances:
            for field, value in data.items():
                setattr(instance, field, value)
        await self._session.flush()
        for instance in instances:
            await self._session.refresh(instance)
        return instances

    async def delete_many(self, *, filters: Sequence[FilterClause]) -> Sequence[ModelT]:
        """Delete every record matching the filters; return the records that were deleted.

        Excludes already-archived rows by default, same as update_many above -- a
        not-yet-or-no-longer-published row is still reachable (see update_many's
        own docstring for why).
        """
        await self._guard_regex_timeout(filters)
        statement = self._matching(filters).where(
            *self._visibility_clauses(include_archived=False, include_unpublished=True)
        )
        result = await self._session.execute(statement)
        instances = result.scalars().all()
        for instance in instances:
            _raise_if_locked(instance, None)
        now = _now()
        for instance in instances:
            if hasattr(instance, "archived_at"):
                instance.archived_at = now
            else:
                await self._session.delete(instance)  # pragma: no cover -- see module docstring
        await self._session.flush()
        for instance in instances:
            if hasattr(instance, "archived_at"):  # pragma: no branch -- see module docstring
                await self._session.refresh(instance)
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
        await self._session.refresh(instance)
        return instance

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[ModelT]:
        """Clear `archived_at` on every record matching the filters; return them."""
        if not hasattr(self._model, "archived_at"):
            return []  # pragma: no cover -- see module docstring
        result = await self._session.execute(self._matching(filters))
        instances = result.scalars().all()
        for instance in instances:
            instance.archived_at = None  # type: ignore[attr-defined]
        await self._session.flush()
        for instance in instances:
            await self._session.refresh(instance)
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
