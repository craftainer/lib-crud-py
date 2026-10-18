"""SQLAlchemy Hero model: the test resource exercising every record-lifecycle mixin."""

from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from craftainer_tool_crud.models.base import IdentifiedBase
from craftainer_tool_crud.models.mixins import Archivable, Draftable, Lockable, Schedulable


class Hero(IdentifiedBase, Archivable, Draftable, Schedulable, Lockable):
    """A hero row in the `heroes` table, owned by the caller (`owner_id`) who created it.

    Exercises every record-lifecycle mixin (see craftainer_tool_crud.models.mixins) --
    `name`/`powers` are nullable so a draft can be created with either or both
    omitted. `power_level` is Hero's one numeric field -- the worked
    example for craftainer_tool_crud.controllers.crud_stats's numeric-field aggregates/forecast
    (`GET <prefix>/stats`'s `numeric` breakdown, `GET <prefix>/predict?field=`);
    every other Hero field is a string or a record-lifecycle flag, so without it
    that field-targeted forecast path would have nothing to exercise end to end.
    """

    __tablename__ = "heroes"

    name: Mapped[str | None] = mapped_column(nullable=True)
    powers: Mapped[list[str] | None] = mapped_column(postgresql.ARRAY(String), nullable=True)
    power_level: Mapped[int | None] = mapped_column(nullable=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
