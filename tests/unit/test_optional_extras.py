"""Unit test: the core imports without the `[mqtt]`/`[xml]` extras, which fail only on use."""

import importlib
import pkgutil
import sys
from typing import Any

import pytest

import craftainer_crud
from craftainer_crud.interfaces.base import MQTTEventSink
from craftainer_crud.xml_codec import from_xml
from tests.support.hero_views import HeroV2Create

_PACKAGE = craftainer_crud.__name__
_MODULES = [
    info.name for info in pkgutil.walk_packages(craftainer_crud.__path__, prefix=f"{_PACKAGE}.")
]


@pytest.fixture
def without_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import aiomqtt` / `import defusedxml` fail and force the package to re-import."""
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            monkeypatch.delitem(sys.modules, name)
    for extra in ("aiomqtt", "defusedxml", "defusedxml.ElementTree", "defusedxml.common"):
        monkeypatch.setitem(sys.modules, extra, None)


@pytest.mark.usefixtures("without_extras")
def test_every_module_imports_without_the_extras() -> None:
    """No module imports aiomqtt or defusedxml at import time."""
    importlib.import_module(_PACKAGE)
    for name in _MODULES:
        importlib.import_module(name)


@pytest.mark.usefixtures("without_extras")
def test_xml_parsing_needs_the_xml_extra() -> None:
    """Parsing an XML body without defusedxml is an ImportError, not a silent fallback."""
    with pytest.raises(ImportError):
        from_xml(b"<hero><name>x</name></hero>", HeroV2Create)


@pytest.mark.usefixtures("without_extras")
async def test_mqtt_publishing_needs_the_mqtt_extra() -> None:
    """Publishing to MQTT without aiomqtt is an ImportError."""
    sink: Any = MQTTEventSink(hostname="broker", port=1883, resource="hero", keepalive=60)
    with pytest.raises(ImportError):
        await sink.publish(resource="hero", record_id=1, action="create", snapshot={})
