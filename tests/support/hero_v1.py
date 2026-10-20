"""Reduced Hero v1: the legacy view and the plain model behind it (manifest fixture).

The model has an owner but none of the lifecycle mixins, so a router over it is
owner-scoped and not schedulable, unlike the full Hero in `hero_model`.
"""

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from craftainer_crud.models.base import IdentifiedBase


class HeroV1Row(IdentifiedBase):
    """A legacy hero row: name, power level and owner only."""

    __tablename__ = "heroes_v1"

    name: Mapped[str | None] = mapped_column(nullable=True)
    power_level: Mapped[int | None] = mapped_column(nullable=True)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)


class HeroV1Create(BaseModel):
    """Fields accepted when creating a v1 Hero."""

    name: str = Field(min_length=1, max_length=200)
    power_level: int | None = None


class HeroV1Update(BaseModel):
    """Fields accepted when updating a v1 Hero -- all optional."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    power_level: int | None = None


class HeroV1(BaseModel):
    """A v1 Hero as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str | None = Field(default=None, min_length=1, max_length=200)
    power_level: int | None = None
    owner_id: str
