"""Response shapes for the generic `GET <prefix>/stats`/`GET <prefix>/predict` routes.

Plain `BaseModel`s, not `ORMView` subclasses (see bulk.py's own docstring for
why): these wrap an already-computed crud.repositories.stats.ResourceStats /
crud.controllers.crud_stats.Prediction, not a raw ORM instance crud.interfaces.
base.CRUDInterface builds one of via `from_attributes`.

Flat, list-of-item sub-models (`NumericFieldStatView`/`CategoricalValueCountView`/
`TimeBucketCountView`) rather than a dict-keyed-by-field-name shape -- this is
what lets crud.controllers.crud_router's XML router render each row as its own
flat model via app.xml_codec.to_xml (which only supports scalar/flat-list
fields, see its own module docstring) and hand-assemble the wrapping tags, the
same pattern already used for a list of records.
"""

from pydantic import BaseModel

from crud.views.base import IXDTFDatetime


class NumericFieldStatView(BaseModel):
    """One numeric field's aggregate statistics: count/min/max/avg/sum."""

    field: str
    count: int
    minimum: float | None
    maximum: float | None
    average: float | None
    total: float | None


class CategoricalValueCountView(BaseModel):
    """One (field, value) pair's count, one row of a categorical field's distribution."""

    field: str
    value: str
    count: int


class TimeBucketCountView(BaseModel):
    """One bucket of a time-bucketed record-count series."""

    bucket_start: IXDTFDatetime
    count: int


class LifecycleStatsView(BaseModel):
    """Record-lifecycle mixin breakdown -- see crud.repositories.stats.LifecycleStats."""

    archived: int | None = None
    draft: int | None = None
    locked: int | None = None
    scheduled_pending: int | None = None
    scheduled_expired: int | None = None


class ResourceStatsView(BaseModel):
    """The full `GET <prefix>/stats` response body."""

    total: int
    numeric: list[NumericFieldStatView]
    categorical: list[CategoricalValueCountView]
    time_series: list[TimeBucketCountView] | None = None
    lifecycle: LifecycleStatsView | None = None


class SeriesPointView(BaseModel):
    """One point of a generic float-valued series -- a known bucket or a projected one."""

    bucket_start: IXDTFDatetime
    value: float


class PredictionView(BaseModel):
    """The full `GET <prefix>/predict` response body.

    `method` is always `"linear_regression"` -- present explicitly so a client
    never mistakes this for a real, trained model (see
    docs/plans/2026-09-crud-stats-and-predictions.md's "Predictions" scope
    decision). `field` is None when the forecast targets record count over time
    (the default) rather than a specific numeric field.
    """

    field: str | None
    bucket: str
    method: str = "linear_regression"
    last_known: SeriesPointView
    predictions: list[SeriesPointView]
