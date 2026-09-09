"""Query parsing and OLS forecasting shared by the generic `/stats`/`/predict` routes.

Numeric/categorical field classification reuses crud.controllers.crud_query.
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
`(bucket_index, value)` pairs, stdlib only (no numpy/scikit-learn) -- see
docs/plans/2026-09-crud-stats-and-predictions.md's "Predictions" scope decision
for why: no new heavy dependency, no trained-model storage/retraining/
versioning concerns, consistent with this template's low-dependency style.
Deliberately generic over *what* series is being forecast (`BucketValue`, a
plain bucket_start/float pair) rather than tied to crud.repositories.stats.
TimeBucketCount's integer `count`: the default (no `field` given) forecasts a
bucketed record-count series (`count_series_as_values` below adapts
Repository.stats's own `time_series`); a `field`-targeted forecast instead sums
that numeric field's values per bucket (`bucket_field_sums`, computed from the
matching records directly -- Repository.stats's own time series is always a
plain count, by design, so a per-field-bucketed sum isn't something the
repository layer already knows how to produce, and isn't worth teaching it for
this one controller-level use).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from crud.controllers.crud_query import FieldKind, field_specs
from crud.repositories.stats import TimeBucket, TimeBucketCount

# A predicted series longer than this would be a near-meaningless naive linear
# projection anyway -- caps the request the same way crud_router._MAX_LIMIT caps
# `?limit=`.
MIN_PERIODS = 1
MAX_PERIODS = 52
DEFAULT_PERIODS = 4

# How many matching records `bucket_field_sums` reads to compute a field-targeted
# forecast's per-bucket sums -- same cap as crud_router.py's own `?limit=` ceiling,
# a sanity bound rather than a precise one (this endpoint is a demo/reporting
# feature, not a paginated listing).
MAX_HISTORY_RECORDS = 1000


class InsufficientHistoryError(Exception):
    """Raised by forecast() when fewer than 2 time buckets of history exist.

    crud.controllers.crud_router's `/predict` route catches this and re-raises it
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

    Mirrors crud.repositories.memory's own `_bucket_start` helper.
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


def forecast(series: Sequence[BucketValue], periods: int, bucket: TimeBucket) -> list[Prediction]:
    """Project `periods` future buckets via ordinary-least-squares linear regression.

    `series` must already be ordered oldest-first (both `count_series_as_values`
    and `bucket_field_sums` above return it that way). Raises
    InsufficientHistoryError for fewer than 2 buckets of history -- a single
    point has no meaningful trend.
    """
    minimum_buckets = 2
    if len(series) < minimum_buckets:
        raise InsufficientHistoryError(
            f"need at least {minimum_buckets} time buckets of history to forecast a trend, "
            f"got {len(series)}"
        )
    xs = list(range(len(series)))
    ys = [item.value for item in series]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = (
        0.0
        if denominator == 0
        else sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator
    )
    intercept = mean_y - slope * mean_x
    last_start = datetime.fromisoformat(series[-1].bucket_start)
    predictions: list[Prediction] = []
    for step in range(1, periods + 1):
        x = len(series) - 1 + step
        value = slope * x + intercept
        bucket_start = _advance(last_start, bucket, step)
        predictions.append(Prediction(bucket_start=bucket_start.isoformat(), value=value))
    return predictions
