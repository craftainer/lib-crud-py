"""Unit test: crud_stats.py's field classification, query parsing, and OLS forecast math.

Uses a small standalone Pydantic schema (not tied to Hero) for the classification/
parsing tests, and plain BucketValue series (independent of any repository) for
forecast() -- proves the math and parsing are correct in isolation, per this
plan's own "resolve test coverage gaps in parallel" convention (this file can be
written independently of the repository-layer work).
"""

from datetime import date, datetime, timedelta
from enum import Enum

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from crud.controllers.crud_stats import (
    BucketValue,
    InsufficientHistoryError,
    bucket_field_sums,
    categorical_fields,
    count_series_as_values,
    forecast,
    numeric_fields,
    parse_bucket,
    parse_predict_field,
)
from crud.repositories.stats import TimeBucket, TimeBucketCount


class _Status(Enum):
    """Plain enum field, to exercise the categorical-classification path."""

    OPEN = "open"
    CLOSED = "closed"


class _Widget(BaseModel):
    """Standalone schema covering every field kind crud_stats classifies."""

    id: int
    label: str
    active: bool
    status: _Status
    weight: float
    created_at: datetime
    released_on: date


class _Point(BaseModel):
    """Minimal record shape for bucket_field_sums, carrying created_at + one numeric field."""

    created_at: datetime
    amount: float | None


# --- Field classification -----------------------------------------------------


def test_numeric_fields_includes_only_plain_int_and_float() -> None:
    """numeric_fields excludes bool/enum/string/date/datetime, despite date/datetime also
    being FieldKind.NUMBER for filtering purposes (see crud_query's own module docstring).
    """
    assert set(numeric_fields(_Widget)) == {"id", "weight"}


def test_categorical_fields_includes_bool_and_enum() -> None:
    """categorical_fields includes boolean and enum fields, nothing else."""
    assert set(categorical_fields(_Widget)) == {"active", "status"}


# --- parse_bucket ---------------------------------------------------------------


def test_parse_bucket_none_returns_none() -> None:
    """A missing `?bucket=` parses as None (time series omitted)."""
    assert parse_bucket(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("day", TimeBucket.DAY), ("week", TimeBucket.WEEK), ("month", TimeBucket.MONTH)],
)
def test_parse_bucket_valid_values(raw: str, expected: TimeBucket) -> None:
    """Each of "day"/"week"/"month" parses to the matching TimeBucket member."""
    assert parse_bucket(raw) is expected


def test_parse_bucket_invalid_value_raises_422() -> None:
    """An unrecognized bucket value is a 422 RequestValidationError, not a 500."""
    with pytest.raises(RequestValidationError):
        parse_bucket("fortnight")


# --- parse_predict_field ---------------------------------------------------------


def test_parse_predict_field_none_returns_none() -> None:
    """A missing `?field=` parses as None (forecast targets record count)."""
    assert parse_predict_field(_Widget, None) is None


def test_parse_predict_field_valid_numeric_field() -> None:
    """A recognized numeric field name is returned unchanged."""
    assert parse_predict_field(_Widget, "weight") == "weight"


def test_parse_predict_field_rejects_non_numeric_field() -> None:
    """A field that exists but isn't numeric (e.g. a bool/string) is rejected (422)."""
    with pytest.raises(RequestValidationError):
        parse_predict_field(_Widget, "label")


def test_parse_predict_field_rejects_unknown_field() -> None:
    """A field name that doesn't exist on the schema at all is rejected (422)."""
    with pytest.raises(RequestValidationError):
        parse_predict_field(_Widget, "not_a_real_field")


# --- count_series_as_values / bucket_field_sums ----------------------------------


def test_count_series_as_values_adapts_counts_to_floats() -> None:
    """count_series_as_values turns a TimeBucketCount series into BucketValues."""
    series = [TimeBucketCount(bucket_start="2024-01-01T00:00:00", count=3)]
    assert count_series_as_values(series) == [
        BucketValue(bucket_start="2024-01-01T00:00:00", value=3.0)
    ]


def test_bucket_field_sums_sums_per_calendar_day() -> None:
    """bucket_field_sums sums `field`'s value per UTC calendar day of created_at."""
    day_one = datetime(2024, 1, 1, 9)
    day_two = datetime(2024, 1, 2, 3)
    records = [
        _Point(created_at=day_one, amount=10),
        _Point(created_at=day_one.replace(hour=20), amount=5),
        _Point(created_at=day_two, amount=7),
    ]
    result = bucket_field_sums(records, "amount", TimeBucket.DAY)
    assert result == [
        BucketValue(bucket_start="2024-01-01T00:00:00", value=15.0),
        BucketValue(bucket_start="2024-01-02T00:00:00", value=7.0),
    ]


def test_bucket_field_sums_buckets_by_week_starting_monday() -> None:
    """WEEK truncates to the Monday of that ISO week, matching Postgres's date_trunc."""
    # 2024-01-03 is a Wednesday; that week's Monday is 2024-01-01.
    records = [_Point(created_at=datetime(2024, 1, 3), amount=1)]
    result = bucket_field_sums(records, "amount", TimeBucket.WEEK)
    assert result == [BucketValue(bucket_start="2024-01-01T00:00:00", value=1.0)]


def test_bucket_field_sums_buckets_by_month() -> None:
    """MONTH truncates to the first day of the calendar month."""
    records = [_Point(created_at=datetime(2024, 1, 15), amount=1)]
    result = bucket_field_sums(records, "amount", TimeBucket.MONTH)
    assert result == [BucketValue(bucket_start="2024-01-01T00:00:00", value=1.0)]


def test_bucket_field_sums_skips_records_with_the_field_unset() -> None:
    """A None-valued field contributes nothing to its bucket, like SQL's SUM() skipping NULL --
    real data is never guaranteed to have every historical record carry an optional field
    (e.g. Hero's power_level).
    """
    day_one = datetime(2024, 1, 1)
    records = [
        _Point(created_at=day_one, amount=10),
        _Point(created_at=day_one.replace(hour=12), amount=None),
    ]
    result = bucket_field_sums(records, "amount", TimeBucket.DAY)
    assert result == [BucketValue(bucket_start="2024-01-01T00:00:00", value=10.0)]


# --- forecast: OLS math -----------------------------------------------------------


def test_forecast_raises_for_fewer_than_two_buckets() -> None:
    """forecast() refuses to project a trend from a single point of history."""
    series = [BucketValue(bucket_start="2024-01-01T00:00:00", value=1.0)]
    with pytest.raises(InsufficientHistoryError):
        forecast(series, periods=1, bucket=TimeBucket.DAY)


def test_forecast_raises_for_empty_series() -> None:
    """forecast() also refuses an empty series."""
    with pytest.raises(InsufficientHistoryError):
        forecast([], periods=1, bucket=TimeBucket.DAY)


def test_forecast_projects_a_perfect_linear_trend() -> None:
    """A perfectly linear series (1, 2, 3, ...) forecasts the exact continuation."""
    series = [
        BucketValue(bucket_start=f"2024-01-0{day}T00:00:00", value=float(day))
        for day in range(1, 4)
    ]
    predictions = forecast(series, periods=2, bucket=TimeBucket.DAY)
    assert [p.value for p in predictions] == pytest.approx([4.0, 5.0])
    assert predictions[0].bucket_start == "2024-01-04T00:00:00"
    assert predictions[1].bucket_start == "2024-01-05T00:00:00"


def test_forecast_flat_series_projects_the_same_value() -> None:
    """A constant series (zero slope) forecasts that same constant forward."""
    series = [
        BucketValue(bucket_start=f"2024-01-0{day}T00:00:00", value=5.0) for day in range(1, 4)
    ]
    predictions = forecast(series, periods=2, bucket=TimeBucket.DAY)
    assert [p.value for p in predictions] == pytest.approx([5.0, 5.0])


def test_forecast_advances_month_buckets_across_a_year_boundary() -> None:
    """A MONTH-bucketed forecast correctly rolls December into next January."""
    series = [
        BucketValue(bucket_start="2023-11-01T00:00:00", value=1.0),
        BucketValue(bucket_start="2023-12-01T00:00:00", value=2.0),
    ]
    predictions = forecast(series, periods=2, bucket=TimeBucket.MONTH)
    assert predictions[0].bucket_start == "2024-01-01T00:00:00"
    assert predictions[1].bucket_start == "2024-02-01T00:00:00"


def test_forecast_advances_week_buckets_by_seven_days() -> None:
    """A WEEK-bucketed forecast advances each projected bucket by exactly 7 days."""
    series = [
        BucketValue(bucket_start="2024-01-01T00:00:00", value=1.0),
        BucketValue(bucket_start="2024-01-08T00:00:00", value=2.0),
    ]
    predictions = forecast(series, periods=1, bucket=TimeBucket.WEEK)
    assert predictions[0].bucket_start == "2024-01-15T00:00:00"
    assert datetime.fromisoformat(predictions[0].bucket_start) - datetime.fromisoformat(
        series[-1].bucket_start
    ) == timedelta(days=7)
