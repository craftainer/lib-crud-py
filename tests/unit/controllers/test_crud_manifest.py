"""Unit test: the manifest route's caller filtering, conditional requests and api-catalog."""

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient

from craftainer_crud.controllers.crud_manifest import api_catalog_router, crud_manifest_router
from craftainer_crud.controllers.crud_router import build_json_router
from craftainer_crud.manifest import MANIFEST_PROFILE, ManifestOptions, ManifestRegistry
from tests.support import quest_views
from tests.unit.conftest import CrudConfigOverride
from tests.unit.manifest.server import FIXTURES, build_server, load_json


def _client() -> TestClient:
    app, _ = build_server()
    return TestClient(app)


def _bearer(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {','.join(roles)}"}


def _names(response: Any) -> list[tuple[str, str]]:  # noqa: ANN401
    return [(r["name"], r["api_version"]) for r in response.json()["resources"]]


def test_manifest_carries_etag_and_revalidation_cache_control() -> None:
    """A 200 has a weak ETag and `Cache-Control: private, no-cache`."""
    response = _client().get("/api/_crud/manifest", headers=_bearer("admin"))
    assert response.status_code == 200
    assert response.headers["etag"].startswith('W/"')
    assert response.headers["cache-control"] == "private, no-cache"


def test_matching_if_none_match_gets_a_bodiless_304() -> None:
    """Repeating the request with the ETag (or `*`) is a 304 carrying the same headers."""
    client = _client()
    first = client.get("/api/_crud/manifest", headers=_bearer("admin"))
    etag = first.headers["etag"]
    for header in (etag, etag.removeprefix("W/"), "*", f'"other", {etag}'):
        again = client.get(
            "/api/_crud/manifest", headers={**_bearer("admin"), "If-None-Match": header}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag
        assert again.headers["cache-control"] == "private, no-cache"


def test_stale_if_none_match_gets_the_full_body() -> None:
    """A non-matching tag is ignored and the manifest is sent again."""
    response = _client().get(
        "/api/_crud/manifest", headers={**_bearer("admin"), "If-None-Match": 'W/"stale"'}
    )
    assert response.status_code == 200
    assert response.json()["manifest_version"] == "1"


def test_etag_is_stable_per_caller_and_differs_between_callers() -> None:
    """The tag covers the caller-filtered body: equal for equal views, unequal otherwise."""
    client = _client()
    tag = {
        who: client.get("/api/_crud/manifest", headers=headers).headers["etag"]
        for who, headers in {
            "admin": _bearer("admin"),
            "admin-again": _bearer("admin"),
            "reader": _bearer("hero-reader"),
            "anonymous": {},
            "stranger": _bearer("unrelated"),
        }.items()
    }
    assert tag["admin"] == tag["admin-again"]
    assert tag["admin"] != tag["reader"]
    assert tag["reader"] != tag["anonymous"]
    assert tag["anonymous"] == tag["stranger"]


def test_a_tag_from_one_caller_does_not_revalidate_for_another() -> None:
    """An anonymous caller's tag never yields a 304 for an admin."""
    client = _client()
    anonymous_tag = client.get("/api/_crud/manifest").headers["etag"]
    response = client.get(
        "/api/_crud/manifest", headers={**_bearer("admin"), "If-None-Match": anonymous_tag}
    )
    assert response.status_code == 200


def test_anonymous_and_stranger_see_only_public_resources() -> None:
    """Hidden resources are omitted, not refused: the response is a 200 listing public ones."""
    client = _client()
    for headers in ({}, _bearer("unrelated")):
        response = client.get("/api/_crud/manifest", headers=headers)
        assert response.status_code == 200
        assert _names(response) == [("quests", "v1")]


def test_resource_registered_as_v1_and_v2_appears_as_two_entries() -> None:
    """v1 and v2 of one name are separate entries, ordered by api_version."""
    response = _client().get("/api/_crud/manifest", headers=_bearer("admin"))
    assert _names(response) == [("heroes", "v1"), ("heroes", "v2"), ("quests", "v1")]


def test_a_reader_of_one_version_does_not_see_the_other() -> None:
    """Role checks are per resource version."""
    legacy = _client().get("/api/_crud/manifest", headers=_bearer("legacy-reader"))
    assert _names(legacy) == [("heroes", "v1"), ("quests", "v1")]


def test_hidden_resource_leaves_no_trace_in_the_body() -> None:
    """An anonymous manifest never mentions a hidden resource's name."""
    text = _client().get("/api/_crud/manifest").text
    assert "/heroes/" not in text
    assert '"name":"heroes"' not in text


def test_manifest_follows_resources_registered_after_the_router_was_built() -> None:
    """The registry is read per request."""
    app, registry = build_server(registry=ManifestRegistry())
    client = TestClient(app)
    before = client.get("/api/_crud/manifest", headers=_bearer("admin")).headers["etag"]
    app.include_router(
        build_json_router(
            prefix="",
            tags=["extra"],
            resource_label="Quest",
            schema=quest_views.Quest,
            create_schema=quest_views.QuestCreate,
            update_schema=quest_views.QuestUpdate,
            crud_dependency=Annotated[Any, Depends(lambda: None)],
            read_roles=Depends(lambda: None),
            write_roles=Depends(lambda: None),
            delete_roles=Depends(lambda: None),
            manifest=ManifestOptions(
                registry=registry,
                name="side-quests",
                api_version="v1",
                public_read=True,
                base_path="/side/json",
            ),
        ),
        prefix="/side/json",
    )
    after = client.get("/api/_crud/manifest", headers=_bearer("admin"))
    assert after.headers["etag"] != before
    assert ("side-quests", "v1") in _names(after)


def test_stats_ttl_and_oidc_url_come_from_the_host() -> None:
    """`stats_ttl` shows only on resources serving stats; the OIDC URL on every resource."""
    body = _client().get("/api/_crud/manifest", headers=_bearer("admin")).json()
    ttls = {(r["name"], r["api_version"]): r["cache"]["stats_ttl"] for r in body["resources"]}
    assert ttls == {("heroes", "v1"): None, ("heroes", "v2"): 30, ("quests", "v1"): None}
    server = load_json(FIXTURES / "server.json")
    assert {r["auth"]["oidc_discovery_url"] for r in body["resources"]} == {
        server["oidc_discovery_url"]
    }


def test_manifest_defaults_stats_ttl_to_unknown_and_honours_prefix_and_config(
    override_crud_config: CrudConfigOverride,
) -> None:
    """No host `stats_ttl` reports null; memory mode has no replay; limits follow the config."""
    override_crud_config(use_memory=True, bulk_action_max_matched=7, sse_keepalive_seconds=2.5)
    _, registry = build_server()
    other = FastAPI()
    other.include_router(
        crud_manifest_router(registry, service_name="s", service_version="1", prefix="/x")
    )
    body = TestClient(other).get("/x/manifest", headers=_bearer("admin")).json()
    heroes_v2 = next(r for r in body["resources"] if r["api_version"] == "v2")
    assert heroes_v2["cache"]["stats_ttl"] is None
    assert heroes_v2["capabilities"]["events"]["replay"] == "none"
    assert heroes_v2["capabilities"]["events"]["keepalive_seconds"] == 2.5
    assert heroes_v2["bulk"]["max_matched"] == 7
    assert heroes_v2["auth"]["oidc_discovery_url"] is None


def _app_with_gate(gate: Any) -> TestClient:  # noqa: ANN401
    registry = ManifestRegistry()
    app = FastAPI()
    app.include_router(
        build_json_router(
            prefix="",
            tags=["quests"],
            resource_label="Quest",
            schema=quest_views.Quest,
            create_schema=quest_views.QuestCreate,
            update_schema=quest_views.QuestUpdate,
            crud_dependency=Annotated[Any, Depends(lambda: None)],
            read_roles=gate,
            write_roles=Depends(lambda: None),
            delete_roles=Depends(lambda: None),
            manifest=ManifestOptions(
                registry=registry, name="quests", api_version="v1", base_path="/quests/json"
            ),
        ),
        prefix="/quests/json",
    )
    app.include_router(crud_manifest_router(registry, service_name="s", service_version="1"))
    return TestClient(app)


def test_a_dependency_with_unsatisfied_parameters_hides_the_resource() -> None:
    """A gate whose own parameters fail validation counts as a refusal."""

    def needs_query(required_param: int) -> int:
        return required_param

    client = _app_with_gate(Depends(needs_query))
    assert client.get("/api/_crud/manifest").json()["resources"] == []


def test_a_gate_that_fails_for_another_reason_is_not_swallowed() -> None:
    """An IdP outage (503) surfaces instead of silently emptying the manifest."""

    def outage() -> None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "IdP unreachable")

    client = _app_with_gate(Depends(outage))
    assert client.get("/api/_crud/manifest").status_code == 503


def test_gate_is_resolved_with_the_apps_dependency_overrides() -> None:
    """The manifest sees what the route sees, including an overridden dependency."""

    def claims(request: Request) -> str:
        return request.headers.get("x-user", "")

    def gate(user: Annotated[str, Depends(claims)]) -> str:
        if user != "ann":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "no")
        return user

    client = _app_with_gate(Depends(gate))
    assert client.get("/api/_crud/manifest").json()["resources"] == []
    assert (
        len(client.get("/api/_crud/manifest", headers={"x-user": "ann"}).json()["resources"]) == 1
    )
    client.app.dependency_overrides[claims] = lambda: "ann"  # type: ignore[attr-defined]
    assert len(client.get("/api/_crud/manifest").json()["resources"]) == 1


def test_manifest_dependency_runs_once_per_request_across_resources() -> None:
    """Two resources behind the same sub-dependency share one evaluation (FastAPI's cache)."""
    calls: list[int] = []

    def claims() -> list[str]:
        calls.append(1)
        return ["admin"]

    registry = ManifestRegistry()
    app = FastAPI()
    for version in ("v1", "v2"):
        app.include_router(
            build_json_router(
                prefix="",
                tags=["quests"],
                resource_label="Quest",
                schema=quest_views.Quest,
                create_schema=quest_views.QuestCreate,
                update_schema=quest_views.QuestUpdate,
                crud_dependency=Annotated[Any, Depends(lambda: None)],
                read_roles=Depends(claims),
                write_roles=Depends(lambda: None),
                delete_roles=Depends(lambda: None),
                manifest=ManifestOptions(
                    registry=registry,
                    name="quests",
                    api_version=version,
                    base_path=f"/{version}/json",
                ),
            ),
            prefix=f"/{version}/json",
        )
    app.include_router(crud_manifest_router(registry, service_name="s", service_version="1"))
    response = TestClient(app).get("/api/_crud/manifest")
    assert [r["api_version"] for r in response.json()["resources"]] == ["v1", "v2"]
    assert len(calls) == 1


def test_api_catalog_is_a_linkset_pointing_at_openapi_and_the_manifest() -> None:
    """RFC 9727: `application/linkset+json` with `service-desc` and a profiled `describedby`."""
    response = _client().get("/.well-known/api-catalog")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/linkset+json")
    assert response.json() == {
        "linkset": [
            {
                "anchor": "http://testserver",
                "service-desc": [
                    {"href": "/openapi.json", "type": "application/vnd.oai.openapi+json"}
                ],
                "describedby": [
                    {
                        "href": "/api/_crud/manifest",
                        "type": "application/json",
                        "profile": MANIFEST_PROFILE,
                    }
                ],
            }
        ]
    }


def test_api_catalog_takes_explicit_anchor_and_openapi_url_and_needs_no_auth() -> None:
    """The anchor and OpenAPI URL are configurable; the catalog is public."""
    app = FastAPI()
    app.include_router(
        api_catalog_router(
            manifest_url="/m", openapi_url="/spec.json", anchor="https://host.example/api"
        )
    )
    body = TestClient(app).get("/.well-known/api-catalog").json()["linkset"][0]
    assert body["anchor"] == "https://host.example/api"
    assert body["service-desc"][0]["href"] == "/spec.json"
    assert body["describedby"][0]["href"] == "/m"


def _catalog_entry(**app_kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
    app = FastAPI(**app_kwargs)
    app.include_router(api_catalog_router(manifest_url="/m"))
    entry: dict[str, Any] = TestClient(app).get("/.well-known/api-catalog").json()["linkset"][0]
    return entry


def test_api_catalog_service_desc_follows_the_apps_openapi_url() -> None:
    """Default: omitted when the app disables OpenAPI, the custom path when it moves it."""
    assert "service-desc" not in _catalog_entry(openapi_url=None)
    assert _catalog_entry(openapi_url="/spec.json")["service-desc"][0]["href"] == "/spec.json"
    assert _catalog_entry()["service-desc"][0]["href"] == "/openapi.json"


def test_api_catalog_default_omits_service_desc_for_an_app_without_openapi_url() -> None:
    """A host app that has no `openapi_url` attribute still gets an answer, without the link."""
    app = FastAPI()
    del app.openapi_url
    app.include_router(api_catalog_router(manifest_url="/m"))
    entry = TestClient(app).get("/.well-known/api-catalog").json()["linkset"][0]
    assert "service-desc" not in entry


def test_api_catalog_openapi_url_none_omits_and_string_overrides_the_app() -> None:
    """An explicit argument wins over the app's setting."""
    app = FastAPI()
    app.include_router(api_catalog_router(manifest_url="/m", openapi_url=None))
    entry = TestClient(app).get("/.well-known/api-catalog").json()["linkset"][0]
    assert "service-desc" not in entry
    assert entry["describedby"][0]["href"] == "/m"
    app = FastAPI(openapi_url=None)
    app.include_router(api_catalog_router(manifest_url="/m", openapi_url="/x.json"))
    entry = TestClient(app).get("/.well-known/api-catalog").json()["linkset"][0]
    assert entry["service-desc"][0]["href"] == "/x.json"


def test_catalog_is_not_in_the_openapi_document_but_the_manifest_is() -> None:
    """The well-known route stays out of the docs; the manifest route is documented."""
    app, _ = build_server()
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert "/api/_crud/manifest" in paths
    assert "/.well-known/api-catalog" not in paths


@pytest.mark.parametrize("path", ["/api/_crud/manifest", "/.well-known/api-catalog"])
def test_only_get_is_allowed(path: str) -> None:
    """Both routes are read-only."""
    assert _client().post(path).status_code == 405
