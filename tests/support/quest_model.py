"""SQLAlchemy Quest model: a public resource with an enum and a date field (manifest fixture)."""

from datetime import date
from enum import StrEnum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from craftainer_crud.models.base import IdentifiedBase


class Difficulty(StrEnum):
    """How hard a quest is; declaration order is the order the manifest lists choices in."""

    EASY = "easy"
    HARD = "hard"
    DEADLY = "deadly"


class Quest(IdentifiedBase):
    """A quest row; no lifecycle mixin, so every optional capability is off."""

    __tablename__ = "quests"

    title: Mapped[str] = mapped_column(String, nullable=False)
    difficulty: Mapped[Difficulty] = mapped_column(String, nullable=False)
    due_on: Mapped[date | None] = mapped_column(nullable=True)
