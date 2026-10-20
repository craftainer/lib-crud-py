"""The Python server passes every CRUD manifest conformance fixture (docs/manifest.md).

For each `tests/fixtures/manifest/<case>.request.json` the app `server.py` builds is asked
for the manifest as that caller; the response must validate against the schema and equal
`<case>.manifest.json`, with the four embedded JSON Schemas compared structurally.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from tests.unit.manifest.server import FIXTURES, ROOT, build_server, load_json, token_headers

CASES = sorted(p.name.removesuffix(".request.json") for p in FIXTURES.glob("*.request.json"))
SCHEMA = load_json(ROOT / "schemas" / "crud-manifest" / "v1.json")
STRUCTURAL = ("read", "create", "update", "draft")


def _shape(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce a JSON Schema to what conformance compares: property names, required, types."""
    if schema is None:
        return None
    return {
        "properties": {n: p.get("type") for n, p in schema.get("properties", {}).items()},
        "required": sorted(schema.get("required", [])),
    }


def _comparable(manifest: dict[str, Any]) -> dict[str, Any]:
    reduced: dict[str, Any] = {**manifest}
    reduced["resources"] = [
        {**r, "schemas": {k: _shape(r["schemas"][k]) for k in STRUCTURAL}}
        for r in manifest["resources"]
    ]
    return reduced


@pytest.mark.parametrize("case", CASES)
def test_server_returns_the_fixture_manifest_for_each_caller(case: str) -> None:
    """The response validates against the schema and equals the case's expected manifest."""
    request = load_json(FIXTURES / f"{case}.request.json")
    expected = load_json(FIXTURES / f"{case}.manifest.json")
    app, _ = build_server()
    response = TestClient(app).request(
        request["method"], request["path"], headers=token_headers(request)
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    actual = response.json()
    Draft202012Validator(SCHEMA).validate(actual)
    assert _comparable(actual) == _comparable(expected)
