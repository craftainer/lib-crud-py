"""Unit test: build_repository_provider's MODE-aware Repository selection."""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import dependency as dependency_module
from app.crud.dependency import build_repository_provider
from app.models.hero import Hero as HeroModel
from app.repositories.memory import InMemoryRepository
from app.repositories.sqlalchemy import SQLAlchemyRepository


def test_mock_mode_shares_one_repository_instance_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under MODE=mock, every call returns the same in-memory repository instance."""
    monkeypatch.setattr(dependency_module.settings, "mode", "mock")
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert first is second
    assert isinstance(first, InMemoryRepository)


def test_non_mock_mode_returns_a_fresh_sqlalchemy_repository_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside MODE=mock, each call returns a new SQLAlchemyRepository bound to the session."""
    monkeypatch.setattr(dependency_module.settings, "mode", "dev")
    provider = build_repository_provider(HeroModel)
    session = cast(AsyncSession, object())

    first = provider(session)
    second = provider(session)

    assert isinstance(first, SQLAlchemyRepository)
    assert isinstance(second, SQLAlchemyRepository)
    assert first is not second
