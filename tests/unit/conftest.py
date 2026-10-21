"""Shared fixtures for tests/unit/craftainer_crud."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from craftainer_crud import config as crud_config
from craftainer_crud.config import CrudConfig, MQTTConfig, get_config

CrudConfigOverride = Callable[..., CrudConfig]


@pytest.fixture
def override_crud_config(monkeypatch: pytest.MonkeyPatch) -> CrudConfigOverride:
    """Return a setter that overrides fields of the installed CrudConfig for one test.

    Keyword names are CrudConfig fields; `mqtt` takes MQTTConfig keyword overrides as a dict.
    The original config is restored afterwards (monkeypatch undoes the module attribute).
    """

    def _override(**changes: Any) -> CrudConfig:  # noqa: ANN401 -- forwarded to dataclasses.replace
        current = get_config()
        mqtt_changes = changes.pop("mqtt", None)
        if mqtt_changes is not None:
            changes["mqtt"] = replace(current.mqtt, **mqtt_changes)
        updated = replace(current, **changes)
        monkeypatch.setattr(crud_config, "_config", updated)
        return updated

    return _override


__all__ = ["MQTTConfig", "override_crud_config"]
