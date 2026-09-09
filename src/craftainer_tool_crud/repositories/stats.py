"""Storage-agnostic statistics vocabulary shared by every Repository implementation.

Plain frozen dataclasses only -- no SQLAlchemy or Python-eval logic lives here,
mirroring filtering.py's own FilterClause/SortClause (see ../README.md). Each
concrete repository (sqlalchemy.py/memory.py) builds a ResourceStats itself, the
same way each interprets FilterClause/SortClause itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class TimeBucket(StrEnum):
    """A calendar-aligned UTC bucket width for a time-bucketed count series.

    UTC calendar buckets (not rolling windows), matching this app's existing
    naive-UTC-everywhere convention (see app/README.md) -- day/week/month
    boundaries as Postgres's own `date_trunc` computes them.
    """

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True)
class NumericFieldStats:
    """Aggregate statistics for one numeric field: count/min/max/avg/sum.

    `count` is the number of non-null values seen for this field among the
    records the stats query matched (may be less than the resource's total).
    """

    field: str
    count: int
    minimum: float | None
    maximum: float | None
    average: float | None
    total: float | None


@dataclass(frozen=True)
class TimeBucketCount:
    """One bucket of a time-bucketed count series: how many records fell in it.

    `bucket_start` is a naive-UTC datetime, ISO-formatted, marking the start of
    the bucket (e.g. midnight UTC for a DAY bucket), matching how this app
    stores every other timestamp (see crud.models.base.IdentifiedBase).
    """

    bucket_start: str
    count: int


@dataclass(frozen=True)
class LifecycleStats:
    """Record-lifecycle mixin breakdown -- see crud.models.mixins.

    Each field is None when the bound model doesn't carry the corresponding
    mixin (detected via `hasattr`, the same pattern crud.repositories.sqlalchemy/
    crud.repositories.memory already use elsewhere) -- a model with no
    record-lifecycle mixins at all gets `LifecycleStats()`, all fields None.
    """

    archived: int | None = None
    draft: int | None = None
    locked: int | None = None
    scheduled_pending: int | None = None
    scheduled_expired: int | None = None


@dataclass(frozen=True)
class ResourceStats:
    """The full statistics payload for one resource, as returned by Repository.stats."""

    total: int
    numeric: dict[str, NumericFieldStats]
    categorical: dict[str, dict[str, int]]
    time_series: Sequence[TimeBucketCount] | None
    lifecycle: LifecycleStats | None
