"""Unit test: craftainer_crud.config's injection point and the session dependency."""

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from craftainer_crud import config as crud_config
from craftainer_crud.config import CrudConfig, configure, get_config, get_session_factory
from craftainer_crud.controllers.crud_router import _rate_limited
from craftainer_crud.models.base import get_db


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo any configure(...) call made by a test."""
    monkeypatch.setattr(crud_config, "_config", crud_config._config)
    monkeypatch.setattr(crud_config, "_session_factory", crud_config._session_factory)


def test_configure_installs_config_and_session_factory() -> None:
    """configure(...) makes both retrievable."""
    factory = cast(async_sessionmaker[AsyncSession], object())
    config = CrudConfig(bulk_action_max_matched=5)

    configure(config, factory)

    assert get_config() is config
    assert get_session_factory() is factory


def test_get_session_factory_raises_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a session factory, asking for one fails loudly."""
    monkeypatch.setattr(crud_config, "_session_factory", None)
    with pytest.raises(RuntimeError, match="configure"):
        get_session_factory()


async def test_get_db_commits_a_session_from_the_injected_factory() -> None:
    """get_db yields a session from the injected factory and commits afterwards."""
    events: list[str] = []

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            events.append("closed")

        async def commit(self) -> None:
            events.append("commit")

    configure(CrudConfig(), cast(Any, _Session))

    gen = get_db()
    session: object = await anext(gen)
    assert isinstance(session, _Session)
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert events == ["commit", "closed"]


def test_rate_limited_applies_the_given_decorator_or_none() -> None:
    """_rate_limited returns the host's decorator, or an identity decorator by default."""

    def route() -> None: ...

    assert _rate_limited(None)(route) is route

    def decorator(func: Any) -> Any:  # noqa: ANN401 -- test double
        return "decorated"

    result: object = _rate_limited(decorator)(route)
    assert result == "decorated"
