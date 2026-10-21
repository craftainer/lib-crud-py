"""Unit test: `build_manifest` derives the document from the code the routers run."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from craftainer_crud.controllers.crud_query import DEFAULT_LIMIT, MAX_LIMIT
from craftainer_crud.manifest import Manifest, RegisteredResource, build_manifest
from tests.support.quest_model import Difficulty


class _Row(BaseModel):
    id: int
    ratio: float
    day: date
    stamp: datetime
    flag: bool
    label: str
    level: Literal["low", "high"]
    kind: Difficulty
    tags: list[str]
    ranks: int | None = None


class _Create(BaseModel):
    label: str
    ratio: float = 1.0


def _resource(**overrides: object) -> RegisteredResource:
    fields: dict[str, object] = {
        "name": "rows",
        "title": "Rows",
        "api_version": "v1",
        "base_path": "/rows/json",
        "formats": {"json": "/rows/json"},
        "schema": _Row,
        "create_schema": _Create,
        "update_schema": _Create,
        "draft_schema": None,
        "archivable": False,
        "revisions": False,
        "events_resource": None,
        "stats": False,
        "schedulable": False,
        "owner_scoped": False,
        "public_read": False,
        "read_gate": None,
        "read_roles": ("reader",),
        "write_roles": (),
        "delete_roles": (),
    }
    fields.update(overrides)
    return RegisteredResource(**fields)  # type: ignore[arg-type]


def _build(*resources: RegisteredResource) -> Manifest:
    return build_manifest(resources, service_name="svc", service_version="9")


def test_value_types_split_dates_from_numbers_and_enums_list_their_choices() -> None:
    """The server's `number` kind becomes integer/number/date/datetime; unfilterable types drop."""
    (resource,) = _build(_resource()).resources
    by_name = {f.name: f for f in resource.fields}
    assert {n: f.value_type for n, f in by_name.items()} == {
        "id": "integer",
        "ratio": "number",
        "day": "date",
        "stamp": "datetime",
        "flag": "boolean",
        "label": "string",
        "level": "enum",
        "kind": "enum",
        "ranks": "integer",
    }
    assert by_name["level"].choices == ["low", "high"]
    assert by_name["kind"].choices == ["easy", "hard", "deadly"]
    assert by_name["label"].choices is None
    assert by_name["ratio"].numeric and not by_name["day"].numeric
    assert by_name["flag"].categorical and by_name["level"].categorical
    assert "tags" not in by_name


def test_read_only_and_required_come_from_the_write_schemas() -> None:
    """A field absent from create and update is read-only; required follows create."""
    (resource,) = _build(_resource()).resources
    by_name = {f.name: f for f in resource.fields}
    assert by_name["id"].read_only and not by_name["id"].required
    assert not by_name["label"].read_only and by_name["label"].required
    assert not by_name["ratio"].read_only and not by_name["ratio"].required


def test_pagination_uses_the_routers_own_limits() -> None:
    """Default and maximum page size are the constants the list routes enforce."""
    (resource,) = _build(_resource()).resources
    assert resource.pagination.default_limit == DEFAULT_LIMIT
    assert resource.pagination.max_limit == MAX_LIMIT
    assert resource.pagination.total is False


def test_optional_routes_appear_in_paths_exactly_when_their_capability_is_on() -> None:
    """`paths` and `capabilities` agree for a bare and a fully featured resource."""
    bare, full = _build(
        _resource(name="a"),
        _resource(
            name="b",
            draft_schema=_Create,
            archivable=True,
            revisions=True,
            stats=True,
            events_resource="row",
        ),
    ).resources
    assert set(bare.paths) == {"filters", "clone"}
    assert bare.capabilities.events is None
    assert set(full.paths) == {
        "filters",
        "stats",
        "predict",
        "revisions",
        "clone",
        "draft",
        "publish",
        "restore",
        "events",
    }
    assert full.capabilities.events is not None
    assert full.capabilities.events.actions == ["create", "update", "delete", "restore"]
    assert full.schemas.draft is not None and bare.schemas.draft is None


def test_public_resource_lists_no_read_roles() -> None:
    """`public_read` empties `read_roles`, whatever the router declared."""
    (resource,) = _build(_resource(public_read=True)).resources
    assert resource.auth.public_read is True
    assert resource.auth.read_roles == []


def test_resources_are_ordered_by_name_then_version() -> None:
    """Registration order does not leak into the document."""
    manifest = _build(
        _resource(name="b", api_version="v1"),
        _resource(name="a", api_version="v2"),
        _resource(name="a", api_version="v1"),
    )
    assert [(r.name, r.api_version) for r in manifest.resources] == [
        ("a", "v1"),
        ("a", "v2"),
        ("b", "v1"),
    ]
