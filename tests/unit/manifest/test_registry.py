"""Unit test: what the router factories register for the manifest."""

from typing import Annotated, Any

import pytest
from fastapi import Depends
from pydantic import BaseModel

from craftainer_crud.controllers.crud_router import build_json_router, build_resource_router
from craftainer_crud.manifest import ManifestOptions, ManifestRegistry
from tests.support import quest_views
from tests.support.hero_model import Hero
from tests.support.hero_v1 import HeroV1Row
from tests.unit.manifest.server import build_server, require_roles

Stub = Annotated[Any, Depends(lambda: None)]


def _json_router(manifest: ManifestOptions | None, **extra: Any) -> None:  # noqa: ANN401
    build_json_router(
        prefix="",
        tags=["quests"],
        resource_label="Quest",
        schema=quest_views.Quest,
        create_schema=quest_views.QuestCreate,
        update_schema=quest_views.QuestUpdate,
        crud_dependency=Stub,
        read_roles=extra.pop("read_roles", Depends(lambda: None)),
        write_roles=Depends(lambda: None),
        delete_roles=Depends(lambda: None),
        manifest=manifest,
        **extra,
    )


def test_the_conformance_server_registers_each_router_once() -> None:
    """Building routers through the factories fills the registry, in build order."""
    _, registry = build_server()
    assert [r.identity for r in registry.resources()] == [
        ("heroes", "v1"),
        ("heroes", "v2"),
        ("quests", "v1"),
    ]


def test_registering_the_same_resource_version_twice_is_an_error() -> None:
    """(name, api_version) is the identity; a second router for it is a wiring bug."""
    registry = ManifestRegistry()
    options = ManifestOptions(registry=registry, name="quests", api_version="v1", base_path="/q")
    _json_router(options)
    with pytest.raises(ValueError, match="'quests' v1 is already registered"):
        _json_router(options)


def test_v1_and_v2_of_one_name_register_separately() -> None:
    """Different api_versions are different resources."""
    registry = ManifestRegistry()
    for version in ("v1", "v2"):
        _json_router(
            ManifestOptions(
                registry=registry, name="quests", api_version=version, base_path=f"/{version}"
            )
        )
    assert [r.api_version for r in registry.resources()] == ["v1", "v2"]


def test_a_router_without_manifest_options_registers_nothing() -> None:
    """The default leaves the router out of every manifest."""
    registry = ManifestRegistry()
    _json_router(None)
    assert registry.resources() == ()


def test_json_router_requires_a_base_path_to_register() -> None:
    """The factory cannot know where the host mounts it, so the host says."""
    options = ManifestOptions(registry=ManifestRegistry(), name="quests", api_version="v1")
    with pytest.raises(ValueError, match="base_path is required"):
        _json_router(options)


def test_title_defaults_to_the_title_cased_name() -> None:
    """Without an explicit title the name is title-cased; an explicit one wins."""
    registry = ManifestRegistry()
    _json_router(
        ManifestOptions(registry=registry, name="side-quests", api_version="v1", base_path="/a")
    )
    _json_router(
        ManifestOptions(
            registry=registry, name="odd", api_version="v1", title="Odd Jobs", base_path="/b"
        )
    )
    assert [r.title for r in registry.resources()] == ["Side-Quests", "Odd Jobs"]


def test_role_names_come_from_the_options_or_the_dependencys_roles_attribute() -> None:
    """Explicit names win; otherwise the dependency callable's `roles` is read; else empty."""
    registry = ManifestRegistry()
    _json_router(
        ManifestOptions(
            registry=registry,
            name="a",
            api_version="v1",
            base_path="/a",
            read_roles=["explicit"],
        ),
        read_roles=require_roles("from-attribute"),
    )
    _json_router(
        ManifestOptions(registry=registry, name="b", api_version="v1", base_path="/b"),
        read_roles=require_roles("from-attribute"),
    )
    _json_router(ManifestOptions(registry=registry, name="c", api_version="v1", base_path="/c"))
    assert [r.read_roles for r in registry.resources()] == [
        ("explicit",),
        ("from-attribute",),
        (),
    ]
    assert all(r.write_roles == () == r.delete_roles for r in registry.resources())


def test_schedulable_is_read_from_the_model() -> None:
    """A Schedulable model is schedulable; a plain one, or none given, is not."""
    registry = ManifestRegistry()
    for name, model in (("a", Hero), ("b", HeroV1Row), ("c", None)):
        _json_router(
            ManifestOptions(
                registry=registry, name=name, api_version="v1", base_path=f"/{name}", model=model
            )
        )
    assert [r.schedulable for r in registry.resources()] == [True, False, False]


def test_event_stream_label_falls_back_to_the_resource_name() -> None:
    """`resource=` labels the stream; without it the manifest name does; no source, no stream."""
    registry = ManifestRegistry()
    for name, extra in (
        ("a", {"event_source_dependency": Stub, "resource": "label"}),
        ("b", {"event_source_dependency": Stub}),
        ("c", {}),
    ):
        _json_router(
            ManifestOptions(registry=registry, name=name, api_version="v1", base_path=f"/{name}"),
            **extra,
        )
    assert [r.events_resource for r in registry.resources()] == ["label", "b", None]


def test_resource_router_registers_every_format_under_its_api_prefix() -> None:
    """`build_resource_router` derives the base path and lists json, xml and web."""
    registry = ManifestRegistry()

    class Item(BaseModel):
        id: int

    build_resource_router(
        prefix="",
        api_prefix="/crud/v1/items/v3",
        tags=["items"],
        resource_label="Item",
        resource="item",
        item_tag="item",
        list_tag="items",
        fields=(),
        schema=Item,
        create_schema=Item,
        update_schema=Item,
        crud_dependency=Stub,
        read_roles=Depends(lambda: None),
        write_roles=Depends(lambda: None),
        delete_roles=Depends(lambda: None),
        manifest=ManifestOptions(registry=registry, name="items", api_version="v3"),
    )
    (resource,) = registry.resources()
    assert resource.base_path == "/crud/v1/items/v3/json"
    assert resource.formats == {
        "json": "/crud/v1/items/v3/json",
        "xml": "/crud/v1/items/v3/xml",
        "web": "/crud/v1/items/v3/web",
    }
    assert not (resource.archivable or resource.stats or resource.revisions)
    assert resource.events_resource is None
