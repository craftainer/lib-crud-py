"""Unit test: `/revisions` and `/events` honour a read-scoped owner (JSON and XML routers).

Two callers, alice and bob, share one repository and one revision table through a
resource built with `OwnerScope(..., read_scoped=True)`. Neither may read the other's
revision history or receive the other's events.
"""

import json
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient

from craftainer_crud.controllers.crud_router import build_json_router, build_xml_router
from craftainer_crud.interfaces.base import (
    CRUDInterface,
    EventIterator,
    EventSource,
    InMemoryEventSink,
    OwnerScope,
    RepositoryRevisionSink,
    _event_envelope,
)
from craftainer_crud.models.revision import Revision
from craftainer_crud.repositories.memory import InMemoryRepository
from tests.support.hero_model import Hero
from tests.support.hero_views import HeroV2, HeroV2Create, HeroV2Update

_heroes = InMemoryRepository(Hero)
_revisions = InMemoryRepository(Revision)
_sink = InMemoryEventSink()


def _get_crud(x_user: Annotated[str, Header()]) -> CRUDInterface[HeroV2, Hero]:
    return CRUDInterface(
        schema=HeroV2,
        repository=_heroes,
        owner=OwnerScope("owner_id", x_user),
        revisions=RepositoryRevisionSink(_revisions),
        events=_sink,
        resource="hero",
        actor=x_user,
    )


CRUD = Annotated[CRUDInterface[HeroV2, Hero], Depends(_get_crud)]
NO_AUTH = Depends(lambda: None)


@dataclass(frozen=True)
class _ReplaySource:
    """EventSource whose subscription replays a fixed list of envelopes, then ends."""

    envelopes: list[dict[str, Any]]

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        """Return a finite iterator over the envelopes (TestClient cannot stream forever)."""
        del durable

        async def replay() -> AsyncGenerator[dict[str, Any]]:
            for envelope in self.envelopes:
                yield envelope

        return subscriber_id or "stub", replay()


_replay = _ReplaySource([])
_SOURCE = Annotated[EventSource, Depends(lambda: _replay)]

_common: dict[str, Any] = {
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
    "revision_repository_dependency": Annotated[
        InMemoryRepository[Revision], Depends(lambda: _revisions)
    ],
    "resource": "hero",
    "event_source_dependency": _SOURCE,
}

app = FastAPI()
app.include_router(build_json_router(**_common), prefix="/heroes")
app.include_router(
    build_xml_router(item_tag="hero", list_tag="heroes", **_common), prefix="/heroes/xml"
)
client = TestClient(app)

ALICE = {"X-User": "alice"}
BOB = {"X-User": "bob"}


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    """Empty the shared repositories around every test."""
    for repository in (_heroes, _revisions):
        repository._records.clear()
        repository._next_id = 1
    _replay.envelopes.clear()
    yield


def _alices_hero() -> int:
    created = client.post("/heroes", json={"name": "Aegis", "powers": ["shield"]}, headers=ALICE)
    assert created.status_code == 201
    return int(created.json()["id"])


@pytest.mark.parametrize("path", ["/heroes/revisions", "/heroes/xml/revisions"])
def test_revisions_of_another_owners_record_are_not_found(path: str) -> None:
    """Bob gets the same 404 as GET for alice's record, and no history; alice still reads it."""
    hero_id = _alices_hero()
    assert client.get("/heroes", params={"id": hero_id}, headers=BOB).status_code == 404

    stolen = client.get(path, params={"id": hero_id}, headers=BOB)
    assert stolen.status_code == 404
    assert stolen.json() == {"detail": "Hero not found"}
    assert "Aegis" not in stolen.text

    own = client.get(path, params={"id": hero_id}, headers=ALICE)
    assert own.status_code == 200
    assert "Aegis" in own.text


def test_revisions_of_an_unknown_record_are_not_found() -> None:
    """A missing id is the same 404 as an invisible one."""
    assert client.get("/heroes/revisions", params={"id": 99}, headers=ALICE).status_code == 404


def _frames(response_text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize("path", ["/heroes/events", "/heroes/xml/events"])
def test_events_are_filtered_per_subscriber(path: str) -> None:
    """A subscriber receives its own owner's events only; an owner-less event is dropped."""
    mine = _event_envelope(
        resource="hero", record_id=1, action="create", snapshot={"name": "A"}, owner="alice"
    )
    theirs = _event_envelope(
        resource="hero", record_id=2, action="create", snapshot={"name": "B"}, owner="bob"
    )
    unowned = {k: v for k, v in theirs.items() if k != "owner"}
    _replay.envelopes.extend([theirs, unowned, mine])

    alice = client.get(path, headers=ALICE)
    frames = _frames(alice.text)
    assert [f.get("record_id") for f in frames[1:]] == [1]  # frames[0] is the subscriber id
    assert "B" not in alice.text


async def test_in_memory_sink_carries_the_owner_from_the_interface() -> None:
    """CRUDInterface publishes the scope's owner into the envelope."""
    sink = InMemoryEventSink()
    _, events = await sink.subscribe(None)
    crud = CRUDInterface(
        schema=HeroV2,
        repository=_heroes,
        owner=OwnerScope("owner_id", "alice"),
        events=sink,
        resource="hero",
    )
    await crud.create(HeroV2Create(name="Aegis", powers=["shield"]))
    envelope = await events.__anext__()
    await events.aclose()
    assert envelope["owner"] == "alice"


@pytest.mark.parametrize("path", ["/heroes", "/heroes/xml"])
@pytest.mark.parametrize(
    "params", [{"skip": -1}, {"limit": 0}, {"limit": -1}, {"skip": 10**12}, {"limit": 1001}]
)
def test_list_rejects_out_of_range_skip_and_limit(path: str, params: dict[str, int]) -> None:
    """Negative/oversized pagination is a 422, not a database error or a cap bypass."""
    assert client.get(path, params=params, headers=ALICE).status_code == 422
