"""Query parsing and OLS forecasting shared by the generic `/stats`/`/predict` routes.

Numeric/categorical field classification reuses craftainer_crud.controllers.crud_query.
field_specs -- the single source of truth for what "kind" a schema field is --
but narrows FieldKind.NUMBER (which also covers date/datetime fields, for
range-filtering purposes -- see crud_query's own module docstring) down to
genuine int/float fields only: averaging or summing a datetime has no sensible
meaning as an aggregate statistic (and errors outright against Postgres), so a
date/datetime field is excluded from `numeric_fields` here rather than reported
with `average`/`total` left at None on every request. FieldKind.BOOLEAN/
FieldKind.ENUM fields become `categorical_fields`, unchanged from field_specs's
own classification.

`forecast()` is a plain ordinary-least-squares linear regression over
`(bucket_offset, value)` pairs (whole buckets since the first bucket -- time, not
position, so gaps do not skew it; exact formula on `forecast`), stdlib only (no
numpy/scikit-learn) -- see
docs/plans/2026-09-crud-stats-and-predictions.md's "Predictions" scope decision
for why: no new heavy dependency, no trained-model storage/retraining/
versioning concerns, consistent with this template's low-dependency style.
Deliberately generic over *what* series is being forecast (`BucketValue`, a
plain bucket_start/float pair) rather than tied to craftainer_crud.repositories.stats.
TimeBucketCount's integer `count`: the default (no `field` given) forecasts a
bucketed record-count series (`count_series_as_values` below adapts
Repository.stats's own `time_series`); a `field`-targeted forecast instead sums
that numeric field's values per bucket (`bucket_field_sums`, computed from the
matching records directly -- Repository.stats's own time series is always a
plain count, by design, so a per-field-bucketed sum isn't something the
repository layer already knows how to produce, and isn't worth teaching it for
this one controller-level use).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from craftainer_crud.controllers.crud_query import FieldKind, field_specs
from craftainer_crud.repositories.stats import TimeBucket, TimeBucketCount

# A predicted series longer than this would be a near-meaningless naive linear
# projection anyway -- caps the request the same way crud_query.MAX_LIMIT caps
# `?limit=`.
MIN_PERIODS = 1
MAX_PERIODS = 52
DEFAULT_PERIODS = 4

# How many matching records `bucket_field_sums` reads to compute a field-targeted
# forecast's per-bucket sums -- same cap as crud_router.py's own `?limit=` ceiling,
# a sanity bound rather than a precise one (this endpoint is a demo/reporting
# feature, not a paginated listing).
MAX_HISTORY_RECORDS = 1000


def cache_scope(crud: object) -> str:
    """Return the caller-scope component of a `StatsCache` key for the given CRUD object.

    Aggregates are shared only between callers who see the same rows: a
    `CRUDInterface` built with a read-scoped `OwnerScope` (ADR-0011) yields one scope
    per owner value, anything else (no owner, or `read_scoped=False` as Hero uses)
    yields the single shared scope. A `CompatCRUD` is looked through to the
    `CRUDInterface` it wraps. Roles are not part of the scope: the route's `read_roles`
    dependency runs before the cache is consulted and decides *whether* a caller may
    read, never *what* they read.
    """
    inner = getattr(crud, "_crud", crud)
    owner = getattr(inner, "_owner", None)
    if owner is None or not owner.read_scoped:
        return "shared"
    # e2e-unreachable: Hero is read_scoped=False; covered by tests/unit/craftainer_crud/
    # controllers/test_crud_router_stats_cache.py
    return f"owner:{owner.field}={owner.value}"  # pragma: no cover


def cache_key(*, kind: str, schema: type[BaseModel], scope: str, params: Mapping[str, Any]) -> str:
    """Return the opaque `StatsCache` key for one `/stats` or `/predict` request.

    A SHA-256 over the route `kind`, the response `schema` (so a deprecated API version
    with a different shape never shares an entry with the current one), the caller
    `scope` (see `cache_scope`) and the already-normalized query `params` -- hashed so
    an owner value never appears in a cache key in clear text. The app version is the
    cache implementation's concern, not part of this key.
    """
    material = json.dumps(
        {
            "kind": kind,
            "schema": f"{schema.__module__}.{schema.__qualname__}",
            "scope": scope,
            "params": dict(params),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


class InsufficientHistoryError(Exception):
    """Raised by forecast() when fewer than 2 time buckets of history exist.

    craftainer_crud.controllers.crud_router's `/predict` route catches this and re-raises it
    as a 422 RequestValidationError, the same status/shape every other
    caller-input problem in this layer uses (see e.g. crud_query.parse_filters).
    """


@dataclass(frozen=True)
class BucketValue:
    """One bucket of a generic float-valued series -- what `forecast` operates over."""

    bucket_start: str
    value: float


@dataclass(frozen=True)
class Prediction:
    """One projected future bucket: its start and the forecast's projected value."""

    bucket_start: str
    value: float


def numeric_fields(schema: type[BaseModel]) -> list[str]:
    """Return `schema` fields eligible for numeric stats/predict: plain int/float only."""
    return [
        name
        for name, spec in field_specs(schema).items()
        if spec.kind == FieldKind.NUMBER
        and isinstance(spec.python_type, type)
        and issubclass(spec.python_type, int | float)
    ]


def categorical_fields(schema: type[BaseModel]) -> list[str]:
    """Return `schema` fields eligible for a categorical value distribution."""
    return [
        name
        for name, spec in field_specs(schema).items()
        if spec.kind in (FieldKind.BOOLEAN, FieldKind.ENUM)
    ]


def parse_bucket(raw: str | None, *, param: str = "bucket") -> TimeBucket | None:
    """Parse a `?bucket=day|week|month` query value, or None if omitted."""
    if raw is None:
        return None
    try:
        return TimeBucket(raw)
    except ValueError as exc:
        errors = [{"loc": ("query", param), "msg": "invalid bucket", "type": "value_error"}]
        raise RequestValidationError(errors) from exc


def parse_predict_field(schema: type[BaseModel], raw: str | None) -> str | None:
    """Validate `?field=` names a numeric field of `schema`, or return None if omitted."""
    if raw is None:
        return None
    if raw not in numeric_fields(schema):
        errors = [{"loc": ("query", "field"), "msg": "not a numeric field", "type": "value_error"}]
        raise RequestValidationError(errors)
    return raw


def count_series_as_values(series: Sequence[TimeBucketCount]) -> list[BucketValue]:
    """Adapt a Repository.stats time_series (record counts) to forecast()'s generic shape."""
    return [BucketValue(bucket_start=item.bucket_start, value=float(item.count)) for item in series]


def bucket_field_sums(
    records: Sequence[BaseModel], field: str, bucket: TimeBucket
) -> list[BucketValue]:
    """Sum `field`'s value per UTC calendar bucket of each record's `created_at`.

    `records` is expected to carry both `created_at` (every ORMView does) and
    `field` (already validated as numeric by `parse_predict_field`). Returned
    oldest-bucket-first, matching Repository.stats's own time_series ordering.
    """
    sums: dict[datetime, float] = {}
    for record in records:
        value = getattr(record, field)
        if value is None:
            # A record with this numeric field left unset contributes nothing to
            # its bucket's sum, the same way SQL's own SUM() skips NULL rows --
            # `field` being nullable is normal (e.g. Hero's power_level defaults
            # to None), not every historical record is guaranteed to carry it.
            continue
        created_at: datetime = record.created_at  # type: ignore[attr-defined]
        start = _bucket_start(bucket, created_at)
        sums[start] = sums.get(start, 0.0) + float(value)
    return [
        BucketValue(bucket_start=start.isoformat(), value=total)
        for start, total in sorted(sums.items())
    ]


def _bucket_start(bucket: TimeBucket, value: datetime) -> datetime:
    """Truncate `value` to the start of its UTC calendar bucket.

    Mirrors craftainer_crud.repositories.memory's own `_bucket_start` helper.
    """
    day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket is TimeBucket.DAY:
        return day_start
    if bucket is TimeBucket.WEEK:
        return day_start - timedelta(days=day_start.weekday())
    return day_start.replace(day=1)


def _advance(start: datetime, bucket: TimeBucket, steps: int) -> datetime:
    """Return `start` advanced by `steps` whole buckets of the given width."""
    if bucket is TimeBucket.DAY:
        return start + timedelta(days=steps)
    if bucket is TimeBucket.WEEK:
        return start + timedelta(weeks=steps)
    month_index = start.month - 1 + steps
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return start.replace(year=year, month=month)


def _bucket_offset(first: datetime, value: datetime, bucket: TimeBucket) -> int:
    """Return how many whole `bucket`s `value` lies after `first` (both bucket starts).

    day: whole days; week: whole days divided by 7 (floor); month: calendar months
    (`12 * year difference + month difference`). Buckets start on a day, Monday or
    first-of-month boundary, so the result is an exact integer, never a rounding.
    """
    if bucket is TimeBucket.MONTH:
        return (value.year - first.year) * 12 + (value.month - first.month)
    days = (value - first).days
    return days // 7 if bucket is TimeBucket.WEEK else days


def forecast(series: Sequence[BucketValue], periods: int, bucket: TimeBucket) -> list[Prediction]:
    """Project `periods` future buckets via ordinary-least-squares linear regression.

    The regression is on bucket *time*, not on position in the series, so a gap (an
    empty bucket, which no server zero-fills) does not skew the trend. With
    `x_i = _bucket_offset(first bucket start, bucket start i)` (a whole number of
    buckets, so x_0 = 0) and `n = len(series)`, evaluated left to right with plain
    accumulating float arithmetic (no `sum`, which is compensated from Python 3.12 and
    would differ in the last bit):

        mean_x    = (x_0 + ... + x_{n-1}) / n
        mean_y    = (y_0 + ... + y_{n-1}) / n
        sxx       = sum((x_i - mean_x) * (x_i - mean_x))
        sxy       = sum((x_i - mean_x) * (y_i - mean_y))
        slope     = 0.0 if sxx == 0 else sxy / sxx
        intercept = mean_y - slope * mean_x
        value_k   = slope * (x_{n-1} + k) + intercept          for k = 1..periods

    The projected bucket k starts k whole buckets after the last one. Raises
    InsufficientHistoryError for fewer than 2 buckets of history -- a single point has
    no meaningful trend. `series` must already be ordered oldest-first (both
    `count_series_as_values` and `bucket_field_sums` above return it that way).
    """
    minimum_buckets = 2
    if len(series) < minimum_buckets:
        raise InsufficientHistoryError(
            f"need at least {minimum_buckets} time buckets of history to forecast a trend, "
            f"got {len(series)}"
        )
    starts = [datetime.fromisoformat(item.bucket_start) for item in series]
    xs = [float(_bucket_offset(starts[0], start, bucket)) for start in starts]
    ys = [item.value for item in series]
    count = float(len(series))
    total_x = 0.0
    total_y = 0.0
    for x, y in zip(xs, ys, strict=True):
        total_x += x
        total_y += y
    mean_x = total_x / count
    mean_y = total_y / count
    sxx = 0.0
    sxy = 0.0
    for x, y in zip(xs, ys, strict=True):
        sxx += (x - mean_x) * (x - mean_x)
        sxy += (x - mean_x) * (y - mean_y)
    slope = 0.0 if sxx == 0 else sxy / sxx
    intercept = mean_y - slope * mean_x
    last_x = xs[-1]
    predictions: list[Prediction] = []
    for step in range(1, periods + 1):
        value = slope * (last_x + step) + intercept
        bucket_start = _advance(starts[-1], bucket, step)
        predictions.append(Prediction(bucket_start=bucket_start.isoformat(), value=value))
    return predictions
