"""Every CRUD manifest conformance fixture validates against schemas/crud-manifest/v1.json.

The fixtures (`tests/fixtures/manifest/`) are the contract's executable examples: the
Python server, the Rust server and both SDKs test against the same files. This test is
the guard that the files themselves stay valid, and that the schema's cross-field rules
(capability on <=> its path present, enum <=> choices) reject what they should.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "schemas" / "crud-manifest" / "v1.json"
FIXTURES = ROOT / "tests" / "fixtures" / "manifest"
CASES = sorted(p.name.removesuffix(".request.json") for p in FIXTURES.glob("*.request.json"))

REQUIRED_CASES = {"anonymous", "hero-reader", "admin"}


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_load(SCHEMA_PATH))


def _admin() -> dict[str, Any]:
    return _load(FIXTURES / "admin.manifest.json")


def _resource(manifest: dict[str, Any], name: str, version: str) -> dict[str, Any]:
    for resource in manifest["resources"]:
        if resource["name"] == name and resource["api_version"] == version:
            found: dict[str, Any] = resource
            return found
    raise AssertionError((name, version))  # pragma: no cover


def test_schema_is_a_valid_2020_12_schema_with_the_profile_id() -> None:
    """The schema is itself valid draft 2020-12 and carries the profile URL as its `$id`."""
    schema = _load(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "https://craftainer.github.io/schemas/crud-manifest/v1"
    assert schema["properties"]["manifest_version"] == {
        "const": "1",
        "description": schema["properties"]["manifest_version"]["description"],
    }


def test_required_cases_exist() -> None:
    """The named cases the contract requires exist, each as a request/manifest pair."""
    assert set(CASES) >= REQUIRED_CASES
    for case in CASES:
        assert (FIXTURES / f"{case}.manifest.json").is_file()


@pytest.mark.parametrize("case", CASES)
def test_fixture_manifest_validates(case: str) -> None:
    """Each expected manifest validates against the schema."""
    manifest = _load(FIXTURES / f"{case}.manifest.json")
    _validator().validate(manifest)


@pytest.mark.parametrize("case", CASES)
def test_fixture_manifest_is_internally_consistent(case: str) -> None:
    """Cross-field rules JSON Schema can't express: ordering, uniqueness, path prefixes."""
    manifest = _load(FIXTURES / f"{case}.manifest.json")
    identities = [(r["name"], r["api_version"]) for r in manifest["resources"]]
    assert identities == sorted(identities)
    assert len(set(identities)) == len(identities)
    for resource in manifest["resources"]:
        assert resource["formats"]["json"] == resource["base_path"]
        assert all(p.startswith(resource["base_path"] + "/") for p in resource["paths"].values())
        events = resource["capabilities"]["events"]
        if events is not None:
            assert events["path"] == resource["paths"]["events"]
        Draft202012Validator.check_schema(resource["schemas"]["read"])
        Draft202012Validator.check_schema(resource["schemas"]["create"])
        Draft202012Validator.check_schema(resource["schemas"]["update"])
        names = [f["name"] for f in resource["fields"]]
        assert len(set(names)) == len(names)
        assert resource["id_field"] in resource["schemas"]["read"]["properties"]


def test_fixtures_visibility_follows_the_caller() -> None:
    """Resources a caller can't read are omitted; public ones are always present."""

    def names(case: str) -> list[tuple[str, str]]:
        manifest = _load(FIXTURES / f"{case}.manifest.json")
        return [(r["name"], r["api_version"]) for r in manifest["resources"]]

    assert names("anonymous") == [("quests", "v1")]
    assert names("hero-reader") == [("heroes", "v2"), ("quests", "v1")]
    assert names("admin") == [("heroes", "v1"), ("heroes", "v2"), ("quests", "v1")]


def test_fixtures_cover_enum_date_and_both_capability_extremes() -> None:
    """Enum, date, all-capabilities-on, none-on and shared-event-stream cases are covered."""
    manifest = _admin()
    quests = _resource(manifest, "quests", "v1")
    by_name = {f["name"]: f for f in quests["fields"]}
    assert by_name["difficulty"]["value_type"] == "enum"
    assert by_name["difficulty"]["choices"] == ["easy", "hard", "deadly"]
    assert by_name["due_on"]["value_type"] == "date"
    assert quests["capabilities"]["events"] is None
    heroes = _resource(manifest, "heroes", "v2")
    every_route = ["filters", "stats", "predict", "revisions", "clone"]
    every_route += ["draft", "publish", "restore", "events"]
    assert set(heroes["paths"]) == set(every_route)
    value_types = {f["value_type"] for f in heroes["fields"]}
    assert value_types >= {"integer", "string", "boolean", "datetime"}
    v1 = _resource(manifest, "heroes", "v1")
    assert v1["capabilities"]["events"]["resource"] == heroes["capabilities"]["events"]["resource"]


def _mutations() -> dict[str, Any]:
    def drop_stats_path(m: dict[str, Any]) -> None:
        del _resource(m, "heroes", "v2")["paths"]["stats"]

    def stats_path_without_capability(m: dict[str, Any]) -> None:
        r = _resource(m, "quests", "v1")
        r["paths"]["stats"] = r["base_path"] + "/stats"

    def enum_without_choices(m: dict[str, Any]) -> None:
        field = next(f for f in _resource(m, "quests", "v1")["fields"] if f["name"] == "difficulty")
        field["choices"] = None

    def choices_on_non_enum(m: dict[str, Any]) -> None:
        _resource(m, "quests", "v1")["fields"][0]["choices"] = ["a"]

    def draft_schema_without_capability(m: dict[str, Any]) -> None:
        _resource(m, "quests", "v1")["schemas"]["draft"] = {"type": "object"}

    def gt_operator(m: dict[str, Any]) -> None:
        _resource(m, "heroes", "v2")["fields"][0]["filter_ops"] = ["gt"]

    def missing_schedulable(m: dict[str, Any]) -> None:
        del _resource(m, "heroes", "v2")["capabilities"]["schedulable"]

    def wrong_version(m: dict[str, Any]) -> None:
        m["manifest_version"] = "2"

    return {
        "stats capability without its path": drop_stats_path,
        "path without its capability": stats_path_without_capability,
        "enum without choices": enum_without_choices,
        "choices on a non-enum": choices_on_non_enum,
        "draft schema without draft_publish": draft_schema_without_capability,
        "gt is not a wire suffix": gt_operator,
        "capability flag missing": missing_schedulable,
        "wrong manifest_version": wrong_version,
    }


@pytest.mark.parametrize("label", list(_mutations()))
def test_schema_rejects_a_broken_manifest(label: str) -> None:
    """Each broken variant of the admin manifest fails validation."""
    manifest = copy.deepcopy(_admin())
    _mutations()[label](manifest)
    with pytest.raises(ValidationError):
        _validator().validate(manifest)


def test_unknown_members_are_accepted() -> None:
    """Forward compatibility: an optional member added later never invalidates a document."""
    manifest = copy.deepcopy(_admin())
    manifest["extra"] = {"anything": 1}
    resource = _resource(manifest, "heroes", "v2")
    resource["default_sort"] = "-created_at"
    resource["fields"][0]["group"] = "identity"
    resource["capabilities"]["relations"] = True
    resource["paths"]["search"] = resource["base_path"] + "/search"
    _validator().validate(manifest)
