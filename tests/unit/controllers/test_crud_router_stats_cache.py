"""Unit test: the optional `stats_cache` on the router factories' `/stats` and `/predict` routes.

Without a cache the routes are untouched (covered by test_crud_router.py); these tests
cover the cached path against a dict-backed StatsCache: miss then hit, JSON and XML,
validation errors never cached, and one entry per ownership scope.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from craftainer_tool_crud.controllers.crud_router import (
    build_json_router,
    build_resource_router,
    build_xml_router,
)
from craftainer_tool_crud.controllers.crud_stats import cache_key, cache_scope
from craftainer_tool_crud.interfaces.base import CRUDInterface, OwnerScope
from craftainer_tool_crud.interfaces.compat import CompatCRUD
from tests.unit.craftainer_tool_crud.controllers.test_crud_router import (
    GadgetCRUD,
    _FakeGadgetRepository,
    _Gadget,
    _GadgetCreate,
    _GadgetUpdate,
    get_gadget_crud,
)

NoAuth = Depends(lambda: None)


class _DictCache:
    """Minimal StatsCache recording its calls."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], bytes] = {}
        self.invalidated: list[str] = []

    async def get(self, resource: str, key: str) -> bytes | None:
        return self.entries.get((resource, key))

    async def set(self, resource: str, key: str, value: bytes) -> None:
        self.entries[(resource, key)] = value

    async def invalidate(self, resource: str) -> None:
        self.invalidated.append(resource)
        self.entries = {k: v for k, v in self.entries.items() if k[0] != resource}


def _common() -> dict[str, object]:
    return {
        "prefix": "",
        "tags": ["gadgets"],
        "resource_label": "Gadget",
        "resource": "gadget",
        "schema": _Gadget,
        "create_schema": _GadgetCreate,
        "update_schema": _GadgetUpdate,
        "crud_dependency": GadgetCRUD,
        "read_roles": NoAuth,
        "write_roles": NoAuth,
        "delete_roles": NoAuth,
        "stats_enabled": True,
    }


_app = FastAPI()  # only its dependency_overrides dict is used, shared with each built app


@pytest.fixture
def cache() -> _DictCache:
    """A fresh dict-backed cache."""
    return _DictCache()


@pytest.fixture
def repository() -> Iterator[_FakeGadgetRepository]:
    """A repository wired into the cached app's CRUD dependency, seeded with two days of data."""
    repo = _FakeGadgetRepository()
    _app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repo
    )
    yield repo
    del _app.dependency_overrides[get_gadget_crud]


def _build_app(cache: _DictCache) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_json_router(**_common(), stats_cache=cache),  # type: ignore[arg-type]
        prefix="/gadgets",
    )
    xml_args = {**_common(), "item_tag": "gadget", "list_tag": "gadgets"}
    app.include_router(
        build_xml_router(**xml_args, stats_cache=cache),  # type: ignore[arg-type]
        prefix="/gadgets/xml",
    )
    app.dependency_overrides = _app.dependency_overrides
    return TestClient(app)


def _seed(repo: _FakeGadgetRepository, client: TestClient) -> None:
    for day, name in enumerate(["apple", "banana", "cherry"], start=1):
        gadget_id = client.post("/gadgets", json={"name": name, "tags": []}).json()["id"]
        repo._records[gadget_id].bucket = f"2024-01-0{day}T00:00:00"


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/gadgets/stats", {"bucket": "day"}),
        ("/gadgets/predict", {"periods": 2}),
        ("/gadgets/xml/stats", {"bucket": "day"}),
        ("/gadgets/xml/predict", {"periods": 2}),
    ],
)
def test_second_request_is_served_from_the_cache(
    path: str, params: dict[str, str | int], cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """A miss stores the body; the next identical request is a byte-identical hit."""
    client = _build_app(cache)
    _seed(repository, client)
    first = client.get(path, params=params)
    assert first.status_code == 200
    assert first.headers["x-stats-cache"] == "miss"
    assert len(cache.entries) == 1
    repository._records.clear()  # a recomputation would now differ (or 422)
    second = client.get(path, params=params)
    assert second.status_code == 200
    assert second.headers["x-stats-cache"] == "hit"
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    assert {resource for resource, _ in cache.entries} == {"gadget"}


def test_cached_json_body_equals_the_uncached_response(
    cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """The cached path serializes exactly what the plain (cache-less) route returns."""
    cached_client = _build_app(cache)
    _seed(repository, cached_client)
    plain_app = FastAPI()
    plain_app.include_router(
        build_json_router(**_common()),  # type: ignore[arg-type]
        prefix="/gadgets",
    )
    plain_app.dependency_overrides = _app.dependency_overrides
    plain = TestClient(plain_app)
    for path in ("/gadgets/stats?bucket=day", "/gadgets/predict"):
        assert cached_client.get(path).content == plain.get(path).content
        assert "x-stats-cache" not in plain.get(path).headers


def test_query_parameters_and_invalidation_change_the_entry(
    cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """Different normalized params get their own entries; invalidate(resource) drops them."""
    client = _build_app(cache)
    _seed(repository, client)
    client.get("/gadgets/stats")
    client.get("/gadgets/stats", params={"bucket": "day"})
    client.get("/gadgets/stats", params={"bucket": "day", "unrelated": "x"})
    assert len(cache.entries) == 2
    client.get("/gadgets/predict")
    client.get("/gadgets/predict", params={"bucket": "day", "periods": 4})
    assert len(cache.entries) == 3
    asyncio.run(cache.invalidate("gadget"))
    assert cache.entries == {}
    assert client.get("/gadgets/stats").headers["x-stats-cache"] == "miss"


def test_invalid_parameters_are_rejected_and_never_cached(
    cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """422s come from the same validation as before and leave the cache empty."""
    client = _build_app(cache)
    _seed(repository, client)
    assert client.get("/gadgets/stats", params={"bucket": "fortnight"}).status_code == 422
    assert client.get("/gadgets/predict", params={"field": "name"}).status_code == 422
    assert client.get("/gadgets/xml/predict", params={"bucket": "x"}).status_code == 422
    assert cache.entries == {}


def test_insufficient_history_is_not_cached(
    cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """A /predict that 422s for lack of history stores nothing."""
    client = _build_app(cache)
    assert client.get("/gadgets/predict").status_code == 422
    assert cache.entries == {}


def test_build_resource_router_forwards_the_cache(
    cache: _DictCache, repository: _FakeGadgetRepository
) -> None:
    """build_resource_router passes stats_cache to its JSON and XML routers."""
    app = FastAPI()
    common = {k: v for k, v in _common().items() if k not in {"prefix"}}
    app.include_router(
        build_resource_router(
            prefix="/r",
            item_tag="gadget",
            list_tag="gadgets",
            fields=("name", "tags"),
            stats_cache=cache,
            **common,  # type: ignore[arg-type]
        )
    )
    app.dependency_overrides = _app.dependency_overrides
    client = TestClient(app)
    assert client.get("/r/json/stats").headers["x-stats-cache"] == "miss"
    assert client.get("/r/xml/stats").headers["x-stats-cache"] == "miss"
    assert len(cache.entries) == 2


def test_stats_cache_requires_a_resource(cache: _DictCache) -> None:
    """A cache without a resource name to invalidate by is a configuration error."""
    args = {k: v for k, v in _common().items() if k != "resource"}
    with pytest.raises(ValueError, match="resource"):
        build_json_router(**args, stats_cache=cache)  # type: ignore[arg-type]


def test_cache_scope_separates_read_scoped_owners() -> None:
    """Shared when unowned or open-read; one scope per owner when reads are owner-scoped."""
    repo = _FakeGadgetRepository()
    plain = CRUDInterface(schema=_Gadget, repository=repo)
    open_read = CRUDInterface(
        schema=_Gadget, repository=repo, owner=OwnerScope("owner_id", "u1", read_scoped=False)
    )
    scoped_a = CRUDInterface(schema=_Gadget, repository=repo, owner=OwnerScope("owner_id", "a"))
    scoped_b = CRUDInterface(schema=_Gadget, repository=repo, owner=OwnerScope("owner_id", "b"))
    assert cache_scope(plain) == cache_scope(open_read) == "shared"
    assert cache_scope(scoped_a) == "owner:owner_id=a"
    assert cache_scope(scoped_a) != cache_scope(scoped_b)
    compat = CompatCRUD(
        scoped_a,
        to_legacy=lambda value: value,
        from_legacy_create=lambda value: value,
        from_legacy_update=lambda value: value,
    )
    assert cache_scope(compat) == cache_scope(scoped_a)
    # Different owners of a read-scoped CRUD therefore never share a cache key.
    params = {"bucket": None}
    key_a = cache_key(kind="stats", schema=_Gadget, scope=cache_scope(scoped_a), params=params)
    key_b = cache_key(kind="stats", schema=_Gadget, scope=cache_scope(scoped_b), params=params)
    assert key_a != key_b


def test_cache_key_depends_on_every_component() -> None:
    """Kind, schema, scope and params each change the key; param order does not."""
    base = {"kind": "stats", "schema": _Gadget, "scope": "shared", "params": {"a": 1, "b": 2}}
    key = cache_key(**base)  # type: ignore[arg-type]
    assert key == cache_key(**{**base, "params": {"b": 2, "a": 1}})  # type: ignore[arg-type]
    assert key != cache_key(**{**base, "kind": "predict"})  # type: ignore[arg-type]
    assert key != cache_key(**{**base, "scope": "owner:x=1"})  # type: ignore[arg-type]
    assert key != cache_key(**{**base, "schema": _GadgetUpdate})  # type: ignore[arg-type]
    assert key != cache_key(**{**base, "params": {"a": 1}})  # type: ignore[arg-type]
