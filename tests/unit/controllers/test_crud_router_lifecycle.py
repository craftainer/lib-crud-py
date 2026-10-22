"""Unit test: the opt-in record-lifecycle routes of the JSON and XML router factories.

Builds both routers with every option on (clone, draft/publish, restore, revisions) over
the in-memory repository and the test Hero resource, then walks one record through its
lifecycle in each format -- the routes a host app's end-to-end tests would otherwise cover.
"""

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from craftainer_crud.controllers.crud_router import (
    build_json_router,
    build_web_router,
    build_xml_router,
)
from craftainer_crud.interfaces.base import CRUDInterface, OwnerScope, RepositoryRevisionSink
from craftainer_crud.models.revision import Revision
from craftainer_crud.repositories.memory import InMemoryRepository
from tests.support.hero_model import Hero
from tests.support.hero_views import HeroV2, HeroV2Create, HeroV2Update

_heroes = InMemoryRepository(Hero)
_revisions = InMemoryRepository(Revision)


def _get_crud() -> CRUDInterface[HeroV2, Hero]:
    return CRUDInterface(
        schema=HeroV2,
        repository=_heroes,
        owner=OwnerScope("owner_id", "tester", read_scoped=False),
        revisions=RepositoryRevisionSink(_revisions),
        resource="hero",
        actor="tester",
    )


def _get_revisions() -> InMemoryRepository[Revision]:
    return _revisions


CRUD = Annotated[CRUDInterface[HeroV2, Hero], Depends(_get_crud)]
REVISIONS = Annotated[InMemoryRepository[Revision], Depends(_get_revisions)]
NO_AUTH = Depends(lambda: None)

_common = {
    "prefix": "",
    "tags": ["heroes"],
    "resource_label": "Hero",
    "schema": HeroV2,
    "create_schema": HeroV2Create,
    "update_schema": HeroV2Update,
    "crud_dependency": CRUD,
    "read_roles": NO_AUTH,
    "write_roles": NO_AUTH,
    "delete_roles": NO_AUTH,
    "draft_schema": HeroV2Update,
    "archivable": True,
    "revision_repository_dependency": REVISIONS,
    "resource": "hero",
    "stats_enabled": True,
}

app = FastAPI()
app.include_router(build_json_router(**_common), prefix="/heroes")
app.include_router(
    build_xml_router(item_tag="hero", list_tag="heroes", **_common),
    prefix="/heroes/xml",
)
app.include_router(
    build_web_router(
        prefix="",
        tags=["heroes"],
        resource="hero",
        api_base="/heroes",
        fields=("name", "powers", "power_level"),
        create_schema=HeroV2Create,
        crud_dependency=CRUD,
        read_roles=NO_AUTH,
        write_roles=NO_AUTH,
    ),
    prefix="/heroes",
)
client = TestClient(app)

_XML_HEADERS = {"Content-Type": "application/xml"}


@pytest.fixture(autouse=True)
def _fresh_repositories() -> Iterator[None]:
    """Empty both in-memory repositories around every test."""
    for repository in (_heroes, _revisions):
        repository._records.clear()
        repository._next_id = 1
    yield


def _hero_xml(name: str) -> bytes:
    return f"<hero><name>{name}</name><powers>Flight</powers></hero>".encode()


def test_json_clone_copies_only_create_fields() -> None:
    """POST /clone duplicates a record; a missing id is a 404."""
    created = client.post("/heroes", json={"name": "Orig", "powers": ["Flight"]}).json()
    cloned = client.post("/heroes/clone", params={"id": created["id"]})
    assert cloned.status_code == 201
    assert cloned.json()["id"] != created["id"]
    assert cloned.json()["name"] == "Orig"
    assert client.post("/heroes/clone", params={"id": 999}).status_code == 404


def test_json_draft_publish_and_revisions() -> None:
    """A draft is saved incomplete, refuses to publish until complete, then publishes."""
    draft = client.post("/heroes/draft", json={"name": "Wip"})
    assert draft.status_code == 201
    hero_id = draft.json()["id"]
    assert draft.json()["is_draft"] is True

    incomplete = client.post("/heroes/publish", params={"id": hero_id})
    assert incomplete.status_code == 422

    client.patch("/heroes", params={"id": hero_id}, json={"powers": ["Flight"]})
    published = client.post("/heroes/publish", params={"id": hero_id})
    assert published.status_code == 200
    assert published.json()["is_draft"] is False
    assert client.post("/heroes/publish", params={"id": 999}).status_code == 404

    revisions = client.get("/heroes/revisions", params={"id": hero_id})
    assert revisions.status_code == 200
    assert revisions.json()[0]["action"] == "update"  # newest first
    assert len(revisions.json()) == 3


def test_json_publish_of_unowned_record_is_404() -> None:
    """A record the caller can see but not write (owner mismatch) 404s instead of a 500."""
    hero = client.post("/heroes", json={"name": "Mine", "powers": ["Flight"]}).json()
    _heroes._records[hero["id"]].owner_id = "someone-else"
    assert client.post("/heroes/publish", params={"id": hero["id"]}).status_code == 404


def test_json_restore_single_and_bulk() -> None:
    """DELETE archives; POST /restore brings back one record by id or many by filter."""
    ids = [
        client.post("/heroes", json={"name": f"H{n}", "powers": ["Flight"]}).json()["id"]
        for n in range(2)
    ]
    for hero_id in ids:
        client.delete("/heroes", params={"id": hero_id})
    assert client.get("/heroes").json() == []
    restored = client.post("/heroes/restore", params={"id": ids[0]})
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    bulk = client.post("/heroes/restore", params={"name": "H1"})
    assert bulk.json()["matched"] == 1
    assert client.post("/heroes/restore", params={"id": 999}).status_code == 404
    assert client.post("/heroes/restore").status_code == 422


def test_xml_clone_draft_publish_restore_and_revisions() -> None:
    """The XML router mirrors the JSON lifecycle routes one-for-one."""
    created = client.post("/heroes/xml", content=_hero_xml("Orig"), headers=_XML_HEADERS)
    assert created.status_code == 201
    hero_id = 1

    cloned = client.post("/heroes/xml/clone", params={"id": hero_id})
    assert cloned.status_code == 201
    assert b"<name>Orig</name>" in cloned.content
    assert client.post("/heroes/xml/clone", params={"id": 999}).status_code == 404

    draft = client.post(
        "/heroes/xml/draft", content=b"<hero><name>Wip</name></hero>", headers=_XML_HEADERS
    )
    assert draft.status_code == 201
    draft_id = 3
    assert client.post("/heroes/xml/publish", params={"id": draft_id}).status_code == 422
    client.patch(
        "/heroes/xml",
        params={"id": draft_id},
        content=b"<hero><powers>Flight</powers></hero>",
        headers=_XML_HEADERS,
    )
    published = client.post("/heroes/xml/publish", params={"id": draft_id})
    assert published.status_code == 200
    assert b"<is_draft>False</is_draft>" in published.content
    assert client.post("/heroes/xml/publish", params={"id": 999}).status_code == 404

    client.delete("/heroes/xml", params={"id": hero_id})
    single = client.post("/heroes/xml/restore", params={"id": hero_id})
    assert single.status_code == 200
    assert b"<name>Orig</name>" in single.content
    client.delete("/heroes/xml", params={"id": hero_id})
    bulk = client.post("/heroes/xml/restore", params={"name": "Orig"})
    assert b"<bulk-update-result>" in bulk.content

    revisions = client.get("/heroes/xml/revisions", params={"id": hero_id})
    assert revisions.status_code == 200
    assert revisions.content.startswith(b"<revisions>")
    assert b"<revision>" in revisions.content


@pytest.mark.parametrize("prefix", ["/heroes", "/heroes/xml"])
def test_every_id_route_rejects_filters_next_to_the_id(prefix: str) -> None:
    """clone, publish, restore and revisions with ?id= plus a filter are a 422, in both formats."""
    created = client.post("/heroes", json={"name": "Orig", "powers": ["Flight"]}).json()
    params = {"id": created["id"], "name": "other"}
    calls = [
        client.post(f"{prefix}/clone", params=params),
        client.post(f"{prefix}/publish", params=params),
        client.post(f"{prefix}/restore", params=params),
        client.get(f"{prefix}/revisions", params=params),
    ]
    for response in calls:
        assert response.status_code == 422, response.text
    assert len(client.get("/heroes").json()) == 1  # nothing was cloned
    # A reserved key next to the id is fine.
    ok = client.get(f"{prefix}/revisions", params={"id": created["id"], "include_archived": "true"})
    assert ok.status_code == 200


def test_xml_publish_of_unowned_record_is_404() -> None:
    """Same guard as the JSON route: an unwritable record 404s."""
    client.post("/heroes/xml", content=_hero_xml("Mine"), headers=_XML_HEADERS)
    _heroes._records[1].owner_id = "someone-else"
    assert client.post("/heroes/xml/publish", params={"id": 1}).status_code == 404


def test_xml_billion_laughs_body_is_a_400() -> None:
    """defusedxml's entity-expansion refusal reaches the client as 400, not a 500."""
    bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><hero><name>&b;</name></hero>'
    response = client.post("/heroes/xml", content=bomb, headers=_XML_HEADERS)
    assert response.status_code == 400


def test_locked_record_refuses_update_and_delete_with_423() -> None:
    """A locked record 423s on single and bulk PATCH/DELETE, in both formats' shared actions."""
    hero = client.post("/heroes", json={"name": "Fixed", "powers": ["Flight"]}).json()
    client.patch("/heroes", params={"id": hero["id"]}, json={"is_locked": True})
    by_id = {"id": hero["id"]}
    by_filter = {"name": "Fixed"}
    for params in (by_id, by_filter):
        assert client.patch("/heroes", params=params, json={"name": "New"}).status_code == 423
        assert client.delete("/heroes", params=params).status_code == 423


def test_predict_without_enough_history_is_422() -> None:
    """/predict needs at least two buckets of history to forecast a trend."""
    client.post("/heroes", json={"name": "One", "powers": ["Flight"]})
    assert client.get("/heroes/predict").status_code == 422
    assert client.get("/heroes/predict", params={"field": "power_level"}).status_code == 422


def test_web_form_blank_optional_field_is_omitted_and_invalid_form_is_422() -> None:
    """A blank optional field falls back to its default; a missing required one 422s."""
    ok = client.post(
        "/heroes/form",
        data={"name": "Formed", "powers": "Flight, Speed", "power_level": ""},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert client.get("/heroes").json()[0]["power_level"] is None
    bad = client.post("/heroes/form", data={"name": "", "powers": ""}, follow_redirects=False)
    assert bad.status_code == 422
