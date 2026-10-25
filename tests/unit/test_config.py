"""Unit test: craftainer_crud.config's injection point and the session dependency."""

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from craftainer_crud import config as crud_config
from craftainer_crud.config import (
    CrudConfig,
    MQTTConfig,
    configure,
    get_config,
    get_session_factory,
)
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


@pytest.mark.parametrize(
    "value", [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), 10**400]
)
def test_sse_keepalive_seconds_rejects_non_positive_or_non_finite(value: float) -> None:
    """0 would spin the keep-alive loop and 0 in the manifest makes clients reconnect forever."""
    with pytest.raises(ValueError, match="sse_keepalive_seconds"):
        CrudConfig(sse_keepalive_seconds=value)


@pytest.mark.parametrize("value", [0.01, 15.0, 3600])
def test_sse_keepalive_seconds_accepts_finite_positive(value: float) -> None:
    """Any finite positive value is kept as given."""
    assert CrudConfig(sse_keepalive_seconds=value).sse_keepalive_seconds == value


@pytest.mark.parametrize("value", [-1, 65536])
def test_mqtt_keepalive_seconds_rejects_out_of_range(value: int) -> None:
    """MQTT 3.1.1 section 3.1.2.10: a 16-bit unsigned value, 0 to 65535."""
    with pytest.raises(ValueError, match="keepalive_seconds"):
        MQTTConfig(keepalive_seconds=value)


@pytest.mark.parametrize("value", [0, 1, 60, 65535])
def test_mqtt_keepalive_seconds_accepts_spec_range(value: int) -> None:
    """0 (keep-alive disabled) through 65535 are valid."""
    assert MQTTConfig(keepalive_seconds=value).keepalive_seconds == value


@pytest.mark.parametrize("value", [None, "15", True])
def test_sse_keepalive_seconds_rejects_non_numbers(value: object) -> None:
    """A non-number (or a bool) is a TypeError, not a confusing comparison failure."""
    with pytest.raises(TypeError, match="sse_keepalive_seconds"):
        CrudConfig(sse_keepalive_seconds=cast(Any, value))


@pytest.mark.parametrize("value", [60.5, "60", None, True])
def test_mqtt_keepalive_seconds_rejects_non_ints(value: object) -> None:
    """The MQTT keep-alive is a 16-bit integer; anything else is a TypeError."""
    with pytest.raises(TypeError, match="keepalive_seconds"):
        MQTTConfig(keepalive_seconds=cast(Any, value))


def test_crud_config_carries_a_valid_mqtt_config() -> None:
    """A CrudConfig embeds an already-validated MQTTConfig."""
    assert CrudConfig(mqtt=MQTTConfig(keepalive_seconds=0)).mqtt.keepalive_seconds == 0
