# crud/repositories/

Storage-agnostic CRUD access that `crud.interfaces`'s generic interface talks
to, parameterized by a model type rather than one class per resource.

- `base.py` — `Repository[ModelT]`, the `Protocol` `crud.interfaces.base.
  CRUDInterface` is written against: `get`/`list`/`create`/`update`/
  `delete`/`count`/`update_many`/`delete_many`/`restore`/`restore_many`/
  `stats`, all storage-agnostic. `list`/`update_many`/`delete_many` take
  `filters`/`sort` sequences from `filtering.py`. Also `RecordLockedError`,
  raised by `update`/`update_many`/`delete`/`delete_many` for a
  `Lockable` (see `../models/README.md`'s `mixins.py`) record whose
  `is_locked` is `True` — except a call whose own `data` itself sets
  `is_locked=False`, which is always let through, so unlocking is a plain
  `update`, never a separate bypass path.
- `filtering.py` — `FilterOp` (the comparison operators a filter can use:
  `EQ`/`NE`/`LT`/`LTE`/`GT`/`GTE`/`IN`/`CONTAINS`/`ICONTAINS`/`REGEX`),
  `FilterClause` (one field/op/value comparison), and `SortClause` (one
  field to sort by). Plain value objects with no SQLAlchemy or
  Python-eval logic — each concrete repository below interprets them
  itself, the same way `Repository` stays a `Protocol` with no shared
  implementation. `crud.controllers.crud_query` is the only place that
  turns an HTTP query string into these (see its own module docstring for
  the wire format); a repository never parses a query string itself.
- `sqlalchemy.py` — `SQLAlchemyRepository[ModelT]`, the default concrete
  implementation: bound to an `AsyncSession` and a `crud.models.base.
  IdentifiedBase` subclass in its constructor, not hardcoded to one
  resource. Translates `FilterClause`/`SortClause` into SQLAlchemy Core
  `where()`/`order_by()` terms via its private `_where_clauses`/
  `_order_by` helpers.
- `memory.py` — `InMemoryRepository[ModelT]`, a dict-backed implementation
  used when `MODE=mock` (see `app.crud_1.heroes.get_hero_crud`) so the
  app needs no database to boot. Matches `sqlalchemy.py`'s shape, but also
  sets `created_at`/`updated_at` itself since there's no server to supply
  them via `server_default`/`onupdate` — as naive UTC datetimes, matching
  the naive `TIMESTAMP` columns Postgres stores them as, so a datetime
  filter compares correctly against either backend. Translates
  `FilterClause`/`SortClause` into plain Python predicates/`sorted()`
  instead.
- `stats.py` — `TimeBucket` (`DAY`/`WEEK`/`MONTH`) and the
  `ResourceStats`/`NumericFieldStats`/`TimeBucketCount`/`LifecycleStats`
  frozen dataclasses `Repository.stats` returns; plain value objects, same
  style as `filtering.py`'s `FilterClause`/`SortClause` — see
  "Statistics" below.

## Record-lifecycle mixins

`get`/`list`/`count` accept `include_archived`/`include_unpublished` (both
default `False`); `SQLAlchemyRepository`/`InMemoryRepository` detect a
bound model's `../models/README.md`'s `mixins.py` mixins via `hasattr`
(the same pattern already used for `created_at`/`updated_at`) and, when
present:

- `Archivable`: `delete`/`delete_many` set `archived_at` instead of
  issuing a real delete (and, for a single `delete`/`update`, treat an
  already-archived row as not found — `False`/`None` — same as one that
  never existed); `restore`/`restore_many` clear it back to `None`;
  `list`/`get`/`count` exclude a row with `archived_at` set unless
  `include_archived=True`. `update_many`/`delete_many` apply this same
  archived-exclusion by default too — a bulk action's filters shouldn't
  silently reach a record a normal read can't see.
- `Schedulable`: `list`/`get`/`count` exclude a row outside its
  `publish_at`/`unpublish_at` window (computed from `datetime.now(UTC)`
  at query time, never a stored boolean) unless `include_unpublished=True`.
  Unlike `Archivable`, `update_many`/`delete_many` do *not* apply this
  exclusion — a not-yet-or-no-longer-published row isn't deleted, just not
  currently visible, and an editor must still be able to correct a
  scheduled record (single `update`/`delete` also stay reachable, for the
  same reason) before it goes live.
- `Lockable`: see `base.py`'s `RecordLockedError`, above.

A model without a given mixin is completely unaffected by all of the
above — no behavior change for a resource that doesn't opt in.

## Statistics

`Repository.stats(*, numeric_fields, categorical_fields, filters=,
bucket=, include_archived=, include_unpublished=)` returns a
`stats.py`-defined `ResourceStats`: the total matching-record count,
per-numeric-field `NumericFieldStats` (count/min/max/avg/sum),
per-categorical-field value-distribution dicts, an optional
`bucket`-width (`stats.TimeBucket.DAY`/`WEEK`/`MONTH`) time-bucketed
`TimeBucketCount` series over `created_at` (omitted, `None`, when `bucket`
isn't given), and — for a model carrying one of the mixins above — a
`LifecycleStats` breakdown (archived/draft/locked/scheduled-pending/
scheduled-expired counts, each `None` if the matching mixin isn't
present; `lifecycle=None` entirely for a model with none of them).
`numeric_fields`/`categorical_fields` are caller-supplied (see
`crud.controllers.crud_stats` for how a resource's own schema derives
them) — this method has no opinion on which fields "should" be
aggregated, only how to aggregate the ones it's given.

- `SQLAlchemyRepository.stats`: one query computing count/min/max/avg/sum
  across every numeric field at once (`sqlalchemy.func`), one `GROUP BY`
  query per categorical field, one `GROUP BY date_trunc(bucket,
  created_at)` query for the time series (only if `bucket` is given), and
  one query with a `COUNT(...) FILTER(WHERE ...)` per present
  lifecycle-mixin column for the breakdown — reusing `_where_clauses`/
  `_visibility_clauses` for filter/archived/unpublished handling exactly
  like `list`/`count` already do.
- `InMemoryRepository.stats`: the equivalent computed in Python over the
  same in-memory dict + existing filter-predicate helpers, matching how it
  already parallels `SQLAlchemyRepository.list`/`count`. Its own
  `_bucket_start` helper truncates a `created_at` value to its UTC
  calendar bucket the same way Postgres's `date_trunc` does (a WEEK bucket
  starts Monday).

Both implementations detect lifecycle mixins via the same `hasattr`
pattern used everywhere else in this module — a model without a given
mixin just gets that field of `LifecycleStats` left `None`, and a model
with none of them gets `lifecycle=None` entirely.

## Do

- Add a new concrete `Repository` implementation here (e.g. for a
  non-SQLAlchemy store) as its own module, matching `sqlalchemy.py`'s
  shape: one class, generic over `ModelT`, taking whatever connection/
  client it needs plus the model/collection type in its constructor.
  Detect record-lifecycle mixins the same `hasattr` way `sqlalchemy.py`/
  `memory.py` already do, rather than requiring every mixin to be
  present.

## Don't

- Import from `crud.interfaces` or `crud.controllers` — see
  `../README.md`'s "Layering" section. `crud.models` is fine
  (`SQLAlchemyRepository` is generic over `IdentifiedBase`).
- Give `SQLAlchemyRepository` resource-specific logic — anything a
  particular resource needs belongs in `crud.interfaces` or the
  resource's own controller calling it, not here.
