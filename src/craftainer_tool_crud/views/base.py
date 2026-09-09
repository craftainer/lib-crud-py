"""Common base for views that convert to and from a SQLAlchemy ORM instance."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer


class ORMView(BaseModel):
    """Base for API-facing Pydantic models constructible from an ORM model instance.

    `from_attributes=True` is what lets crud.interfaces.base.CRUDInterface build one of these
    straight from a SQLAlchemy model instance via `model_validate`, instead of a dict.
    """

    model_config = ConfigDict(from_attributes=True)


def _to_ixdtf(value: datetime) -> str:
    """Format a datetime as an RFC 9557 IXDTF string with a bracketed zone suffix.

    Every timestamp this app stores is UTC (see crud.models.base.IdentifiedBase), so
    the suffix is always the fixed `[UTC]` zone name rather than a per-value lookup.
    """
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") + "[UTC]"


def _to_naive_utc(value: datetime) -> datetime:
    """Normalize a tz-aware datetime to naive UTC; a naive value is passed through as-is.

    Every timestamp this app stores is naive-but-conceptually-UTC (see
    crud.models.base.IdentifiedBase), so a tz-aware value accepted on an input view
    (e.g. a Schedulable field like `publish_at` on app.views.hero_v2.HeroV2Update)
    needs the same normalization crud.controllers.crud_query._cast_datetime already
    applies to a tz-aware filter value -- otherwise comparing it against a naive
    column raises outright (SQLAlchemyRepository) or against a naive `now()`
    (InMemoryRepository). A value already naive here (e.g. an ORM instance's own
    column, read back through `model_validate`) is assumed already UTC, unchanged.
    """
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


IXDTFDatetime = Annotated[
    datetime,
    AfterValidator(_to_naive_utc),
    PlainSerializer(_to_ixdtf, return_type=str, when_used="json"),
]
"""`when_used="json"`: a plain `.model_dump()` (python mode) keeps a real `datetime`
object rather than this IXDTF string -- required since crud.interfaces.base.
CRUDInterface.create/update/update_many feed a view's own `.model_dump(
exclude_unset=True)` straight into a repository write, which needs a real
`datetime` to compare/store, not a string. `.model_dump(mode="json")` (used by
FastAPI's own response encoding, and explicitly by app.xml_codec.to_xml/app.
interfaces.base's revision-snapshot dump) is unaffected -- it still serializes
through `_to_ixdtf` either way.
"""
