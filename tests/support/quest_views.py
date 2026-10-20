"""Pydantic view models for the test Quest resource."""

from datetime import date

from pydantic import BaseModel, ConfigDict

from tests.support.quest_model import Difficulty


class QuestCreate(BaseModel):
    """Fields accepted when creating a Quest."""

    title: str
    difficulty: Difficulty
    due_on: date | None = None


class QuestUpdate(BaseModel):
    """Fields accepted when updating a Quest -- all optional."""

    title: str | None = None
    difficulty: Difficulty | None = None
    due_on: date | None = None


class Quest(BaseModel):
    """A Quest as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    difficulty: Difficulty
    due_on: date | None = None
