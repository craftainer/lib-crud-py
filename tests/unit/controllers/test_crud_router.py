"""Unit test: build_json_router/build_xml_router/build_web_router's generic route wiring.

Exercises the three router factories directly against a minimal fake schema/model
pair, not tied to Hero -- mirrors how tests/unit/crud/interfaces/test_compat.py tests
CompatCRUD generically.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from crud.controllers import crud_actions
from crud.controllers import crud_router as crud_router_module
from crud.controllers.crud_router import build_json_router, build_web_router, build_xml_router
from crud.interfaces.base import CRUDInterface, EventSource, InMemoryEventSink
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.stats import NumericFieldStats, ResourceStats, TimeBucket, TimeBucketCount


@dataclass
class _GadgetRecord:
    """Stand-in for a persisted record, independent of any ORM.

    `created_at`/`bucket` are only used by `_FakeGadgetRepository.stats`'s
    time-bucketed series -- unlike a real IdentifiedBase model, this stand-in
    lets a test set `bucket` directly rather than computing a real UTC calendar
    bucket from a timestamp, keeping the stats/predict tests below deterministic.
    """

    id: int
    name: str
    tags: list[str]
    bucket: str = "2024-01-01T00:00:00"


class _Gadget(BaseModel):
    """View returned by the CRUD interface."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    tags: list[str]


class _GadgetCreate(BaseModel):
    """View accepted when creating a gadget."""

    name: str
    tags: list[str]


class _GadgetUpdate(BaseModel):
    """View accepted when updating a gadget -- all optional."""

    name: str | None = None
    tags: list[str] | None = None


def _matches(record: _GadgetRecord, clause: FilterClause) -> bool:
    value = getattr(record, clause.field)
    if clause.op is FilterOp.EQ:
        return bool(value == clause.value)
    if clause.op is FilterOp.ICONTAINS:
        return str(clause.value).casefold() in str(value).casefold()
    if clause.op is FilterOp.IN:
        return value in clause.value
    raise NotImplementedError(clause.op)


class _FakeGadgetRepository:
    """In-memory Repository implementation, keyed by id."""

    def __init__(self) -> None:
        """Start with no records and the first id to hand out."""
        self._records: dict[int, _GadgetRecord] = {}
        self._next_id = 1

    def _matching(self, filters: Sequence[FilterClause]) -> list[_GadgetRecord]:
        return [r for r in self._records.values() if all(_matches(r, c) for c in filters)]

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> _GadgetRecord | None:
        """Return the record with the given id, or None if it doesn't exist."""
        return self._records.get(record_id)

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> list[_GadgetRecord]:
        """Return up to `limit` matching records, skipping the first `skip`."""
        matching = self._matching(filters)
        for clause in reversed(sort):
            matching = sorted(
                matching, key=lambda r: getattr(r, clause.field), reverse=clause.descending
            )
        return matching[skip : skip + limit]

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters."""
        return len(self._matching(filters))

    async def restore(self, record_id: int) -> _GadgetRecord | None:
        """No Archivable field on _GadgetRecord -- nothing to restore, ever."""
        return None

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[_GadgetRecord]:
        """No Archivable field on _GadgetRecord -- nothing to restore, ever."""
        return []

    async def create(self, data: dict[str, Any]) -> _GadgetRecord:
        """Create and return a new record from the given field values."""
        record = _GadgetRecord(id=self._next_id, **data)
        self._records[self._next_id] = record
        self._next_id += 1
        return record

    async def update(self, record_id: int, data: dict[str, Any]) -> _GadgetRecord | None:
        """Update the record with the given id and return it, or None if it doesn't exist."""
        record = self._records.get(record_id)
        if record is None:
            return None
        for field, value in data.items():
            setattr(record, field, value)
        return record

    async def delete(self, record_id: int) -> bool:
        """Delete the record with the given id; return whether it existed."""
        return self._records.pop(record_id, None) is not None

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: dict[str, Any]
    ) -> Sequence[_GadgetRecord]:
        """Apply the given field values to every record matching the filters; return them."""
        matching = self._matching(filters)
        for record in matching:
            for field, value in data.items():
                setattr(record, field, value)
        return matching

    async def delete_many(self, *, filters: Sequence[FilterClause]) -> Sequence[_GadgetRecord]:
        """Delete every record matching the filters; return the records that were deleted."""
        matching = self._matching(filters)
        for record in matching:
            del self._records[record.id]
        return matching

    async def stats(
        self,
        *,
        numeric_fields: Sequence[str],
        categorical_fields: Sequence[str],
        filters: Sequence[FilterClause] = (),
        bucket: TimeBucket | None = None,
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> ResourceStats:
        """Minimal stand-in for Repository.stats -- no lifecycle mixin on _GadgetRecord."""
        matching = self._matching(filters)
        numeric: dict[str, NumericFieldStats] = {}
        for field in numeric_fields:
            values = [float(getattr(r, field)) for r in matching]
            numeric[field] = NumericFieldStats(
                field=field,
                count=len(values),
                minimum=min(values) if values else None,
                maximum=max(values) if values else None,
                average=(sum(values) / len(values)) if values else None,
                total=sum(values) if values else None,
            )
        categorical: dict[str, dict[str, int]] = {field: {} for field in categorical_fields}
        time_series = None
        if bucket is not None:
            counts: dict[str, int] = {}
            for record in matching:
                counts[record.bucket] = counts.get(record.bucket, 0) + 1
            time_series = [
                TimeBucketCount(bucket_start=start, count=count)
                for start, count in sorted(counts.items())
            ]
        return ResourceStats(
            total=len(matching),
            numeric=numeric,
            categorical=categorical,
            time_series=time_series,
            lifecycle=None,
        )


def get_gadget_crud() -> CRUDInterface[_Gadget, _GadgetRecord]:
    """Build a CRUD interface for Gadget (always overridden per test, never called as-is)."""
    return CRUDInterface(schema=_Gadget, repository=_FakeGadgetRepository())


GadgetCRUD = Annotated[CRUDInterface[_Gadget, _GadgetRecord], Depends(get_gadget_crud)]

# No RBAC of its own to exercise here (see tests/unit/controllers/test_heroes.py for that) --
# a single always-succeeding dependency stands in for read/write/delete roles alike.
NoAuth = Depends(lambda: None)

app = FastAPI()
app.include_router(
    build_json_router(
        prefix="",
        tags=["gadgets"],
        resource_label="Gadget",
        schema=_Gadget,
        create_schema=_GadgetCreate,
        update_schema=_GadgetUpdate,
        crud_dependency=GadgetCRUD,
        read_roles=NoAuth,
        write_roles=NoAuth,
        delete_roles=NoAuth,
        stats_enabled=True,
    ),
    prefix="/gadgets",
)
app.include_router(
    build_xml_router(
        prefix="",
        tags=["gadgets"],
        resource_label="Gadget",
        item_tag="gadget",
        list_tag="gadgets",
        schema=_Gadget,
        create_schema=_GadgetCreate,
        update_schema=_GadgetUpdate,
        crud_dependency=GadgetCRUD,
        read_roles=NoAuth,
        write_roles=NoAuth,
        delete_roles=NoAuth,
        stats_enabled=True,
    ),
    prefix="/gadgets/xml",
)
app.include_router(
    build_web_router(
        prefix="",
        tags=["gadgets"],
        resource="gadget",
        api_base="/gadgets",
        fields=("name", "tags"),
        create_schema=_GadgetCreate,
        crud_dependency=GadgetCRUD,
        read_roles=NoAuth,
        write_roles=NoAuth,
    ),
    prefix="/gadgets",
)

client = TestClient(app)


def test_json_router_crud_lifecycle() -> None:
    """Create, list, get, update, and delete a record through the generated JSON routes."""
    # One repository instance shared across every request in this test -- FastAPI
    # calls the override afresh per request, so a per-call repository would silently
    # discard state between requests.
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        create_response = client.post("/gadgets", json={"name": "Widget", "tags": ["a"]})
        assert create_response.status_code == 201
        gadget_id = create_response.json()["id"]

        list_response = client.get("/gadgets")
        assert list_response.status_code == 200
        assert len(list_response.json()) == 1

        get_response = client.get("/gadgets", params={"id": gadget_id})
        assert get_response.status_code == 200
        assert get_response.json()["name"] == "Widget"

        update_response = client.patch("/gadgets", params={"id": gadget_id}, json={"tags": ["b"]})
        assert update_response.status_code == 200
        assert update_response.json()["tags"] == ["b"]

        delete_response = client.delete("/gadgets", params={"id": gadget_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/gadgets", params={"id": gadget_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_get_missing_returns_404() -> None:
    """GET /gadgets?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=_FakeGadgetRepository()
    )
    try:
        response = client.get("/gadgets", params={"id": 999})
    finally:
        del app.dependency_overrides[get_gadget_crud]
    assert response.status_code == 404


def test_json_router_update_missing_returns_404() -> None:
    """PATCH /gadgets?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=_FakeGadgetRepository()
    )
    try:
        response = client.patch("/gadgets", params={"id": 999}, json={"name": "Nobody"})
    finally:
        del app.dependency_overrides[get_gadget_crud]
    assert response.status_code == 404


def test_json_router_delete_missing_returns_404() -> None:
    """DELETE /gadgets?id= for a nonexistent id returns 404."""
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=_FakeGadgetRepository()
    )
    try:
        response = client.delete("/gadgets", params={"id": 999})
    finally:
        del app.dependency_overrides[get_gadget_crud]
    assert response.status_code == 404


def test_json_router_list_applies_filters_and_sort() -> None:
    """GET /gadgets?name=...&sort=... filters and sorts the list."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "apricot", "tags": []})
        client.post("/gadgets", json={"name": "banana", "tags": []})

        response = client.get("/gadgets", params={"name__icontains": "ap", "sort": "-name"})
        assert response.status_code == 200
        assert [g["name"] for g in response.json()] == ["apricot", "apple"]
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_list_rejects_limit_over_the_cap() -> None:
    """GET /gadgets?limit=1001 is a 422, capping how many records one request can pull."""
    response = client.get("/gadgets", params={"limit": 1001})
    assert response.status_code == 422


def test_json_router_list_allows_limit_at_the_cap() -> None:
    """GET /gadgets?limit=1000 (the cap itself) is accepted."""
    response = client.get("/gadgets", params={"limit": 1000})
    assert response.status_code == 200


def test_json_router_bulk_update_via_filters() -> None:
    """PATCH /gadgets?<filters> with no id updates every matching record."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "apricot", "tags": []})
        client.post("/gadgets", json={"name": "banana", "tags": []})

        response = client.patch(
            "/gadgets", params={"name__icontains": "ap"}, json={"tags": ["updated"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["matched"] == 2
        assert len(body["ids"]) == 2

        remaining = client.get("/gadgets").json()
        tags = sorted((g["tags"][0] if g["tags"] else "") for g in remaining)
        assert tags == ["", "updated", "updated"]
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_delete_via_filters() -> None:
    """DELETE /gadgets?<filters> with no id deletes every matching record."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "apricot", "tags": []})
        client.post("/gadgets", json={"name": "banana", "tags": []})

        response = client.delete("/gadgets", params={"name__icontains": "ap"})
        assert response.status_code == 200
        body = response.json()
        assert body["matched"] == 2

        remaining = client.get("/gadgets").json()
        assert [g["name"] for g in remaining] == ["banana"]
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_update_rejected_over_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bulk PATCH matching more records than bulk_action_max_matched is refused, untouched."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    monkeypatch.setattr(crud_actions.settings, "bulk_action_max_matched", 1)
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "apricot", "tags": []})

        response = client.patch(
            "/gadgets", params={"name__icontains": "ap"}, json={"tags": ["updated"]}
        )
        assert response.status_code == 400

        remaining = client.get("/gadgets").json()
        assert all(g["tags"] == [] for g in remaining)
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_delete_rejected_over_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bulk DELETE matching more records than bulk_action_max_matched is refused, untouched."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    monkeypatch.setattr(crud_actions.settings, "bulk_action_max_matched", 1)
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "apricot", "tags": []})

        response = client.delete("/gadgets", params={"name__icontains": "ap"})
        assert response.status_code == 400
        assert len(client.get("/gadgets").json()) == 2
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_update_logs_an_audit_entry(caplog: pytest.LogCaptureFixture) -> None:
    """A successful bulk update logs an audit entry naming the filters and affected ids."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        with caplog.at_level(logging.INFO, logger="crud.controllers.crud_actions"):
            response = client.patch(
                "/gadgets", params={"name__icontains": "ap"}, json={"tags": ["updated"]}
            )
        assert response.status_code == 200
        assert "Bulk update: actor=unknown path=/gadgets" in caplog.text
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_delete_logs_an_audit_entry(caplog: pytest.LogCaptureFixture) -> None:
    """A successful bulk delete logs an audit entry naming the filters and affected ids."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        with caplog.at_level(logging.INFO, logger="crud.controllers.crud_actions"):
            response = client.delete("/gadgets", params={"name__icontains": "ap"})
        assert response.status_code == 200
        assert "Bulk delete: actor=unknown path=/gadgets" in caplog.text
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_bulk_update_with_no_filters_and_no_id_rejected() -> None:
    """PATCH /gadgets with neither id nor filters is rejected (400), never a full-table update."""
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=_FakeGadgetRepository()
    )
    try:
        response = client.patch("/gadgets", json={"name": "Nobody"})
    finally:
        del app.dependency_overrides[get_gadget_crud]
    assert response.status_code == 422


def test_json_router_bulk_delete_with_no_filters_and_no_id_rejected() -> None:
    """DELETE /gadgets with neither id nor filters is rejected (400), never a full-table delete."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        response = client.delete("/gadgets")
        assert response.status_code == 422
        assert client.get("/gadgets").json() != []
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_xml_router_crud_lifecycle() -> None:
    """Create, get, and delete a record through the generated XML routes."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        create_response = client.post(
            "/gadgets/xml",
            content="<gadget><name>Widget</name><tags>a</tags></gadget>",
            headers={"Content-Type": "application/xml"},
        )
        assert create_response.status_code == 201
        assert create_response.headers["content-type"] == "application/xml"
        assert "<name>Widget</name>" in create_response.text

        list_response = client.get("/gadgets/xml")
        assert list_response.status_code == 200
        assert "<gadgets>" in list_response.text
        gadget_id = list_response.text.split("<id>")[1].split("</id>")[0]

        get_response = client.get("/gadgets/xml", params={"id": gadget_id})
        assert get_response.status_code == 200
        assert "<name>Widget</name>" in get_response.text

        delete_response = client.delete("/gadgets/xml", params={"id": gadget_id})
        assert delete_response.status_code == 204

        missing_response = client.get("/gadgets/xml", params={"id": gadget_id})
        assert missing_response.status_code == 404
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_xml_router_bulk_update_and_delete_via_filters() -> None:
    """PATCH/DELETE /gadgets/xml?<filters> act in bulk and render an XML bulk-result body."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post(
            "/gadgets/xml",
            content="<gadget><name>apple</name><tags>a</tags></gadget>",
            headers={"Content-Type": "application/xml"},
        )
        client.post(
            "/gadgets/xml",
            content="<gadget><name>apricot</name><tags>a</tags></gadget>",
            headers={"Content-Type": "application/xml"},
        )

        update_response = client.patch(
            "/gadgets/xml",
            params={"name__icontains": "ap"},
            content="<gadget><tags>updated</tags></gadget>",
            headers={"Content-Type": "application/xml"},
        )
        assert update_response.status_code == 200
        assert "<bulk-update-result>" in update_response.text
        assert "<matched>2</matched>" in update_response.text

        delete_response = client.delete("/gadgets/xml", params={"name__icontains": "ap"})
        assert delete_response.status_code == 200
        assert "<bulk-delete-result>" in delete_response.text
        assert "<matched>2</matched>" in delete_response.text
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_web_router_form_page_serves_html() -> None:
    """GET /gadgets/form serves an HTML page with the form and web-component tags."""
    response = client.get("/gadgets/form")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text
    assert "<gadget-list" in response.text


def test_web_router_submit_form_splits_list_field_on_comma() -> None:
    """POST /gadgets/form comma-splits a list field's raw value before creating the record."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        response = client.post(
            "/gadgets/form",
            data={"name": "Widget", "tags": "a, b, c"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        list_response = client.get("/gadgets")
        assert list_response.json()[0]["tags"] == ["a", "b", "c"]
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_web_router_components_js_defines_custom_elements() -> None:
    """GET /gadgets/components.js serves the web-component JS."""
    response = client.get("/gadgets/components.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "customElements.define" in response.text


# --- `/events` SSE route: opt-in via event_source_dependency ------------------


def _require_viewer_header(x_role: str | None = Header(default=None)) -> None:
    """403 unless `X-Role: viewer` is present -- a tiny stand-in for app.oidc.require_roles,
    just enough to prove `/events` is gated by the same dependency object the plain `GET`
    list route already uses.
    """
    if x_role != "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


_events_event_sink = InMemoryEventSink()
_EventsReadRoles = Depends(_require_viewer_header)


def get_gadget_event_source() -> EventSource:
    """Return the shared InMemoryEventSink for this test app's events-enabled router."""
    return _events_event_sink


GadgetEventSource = Annotated[EventSource, Depends(get_gadget_event_source)]

events_app = FastAPI()
events_app.include_router(
    build_json_router(
        prefix="",
        tags=["gadgets"],
        resource_label="Gadget",
        schema=_Gadget,
        create_schema=_GadgetCreate,
        update_schema=_GadgetUpdate,
        crud_dependency=GadgetCRUD,
        read_roles=_EventsReadRoles,
        write_roles=NoAuth,
        delete_roles=NoAuth,
        event_source_dependency=GadgetEventSource,
    ),
    prefix="/gadgets",
)
# Also mounts archivable/draft_schema/event_source_dependency on the XML router, to
# exercise build_xml_router's own restore/draft/publish/events parity routes (see
# tests below) -- a separate app from `app`/`client` above so it doesn't affect
# their own baseline (no archivable/draft/events there).
events_app.include_router(
    build_xml_router(
        prefix="",
        tags=["gadgets"],
        resource_label="Gadget",
        item_tag="gadget",
        list_tag="gadgets",
        schema=_Gadget,
        create_schema=_GadgetCreate,
        update_schema=_GadgetUpdate,
        crud_dependency=GadgetCRUD,
        read_roles=_EventsReadRoles,
        write_roles=NoAuth,
        delete_roles=NoAuth,
        archivable=True,
        draft_schema=_GadgetUpdate,
        event_source_dependency=GadgetEventSource,
    ),
    prefix="/gadgets/xml",
)
events_client = TestClient(events_app)
events_app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
    schema=_Gadget, repository=_FakeGadgetRepository()
)


async def _no_events_ever() -> AsyncIterator[dict[str, Any]]:
    """An EventSource iterator that ends immediately, yielding nothing.

    Both TestClient (a portal that runs the whole request to completion before
    returning anything, see starlette.testclient._TestClientTransport) and
    httpx.ASGITransport (which likewise awaits the whole ASGI app call before
    returning a Response, see httpx._transports.asgi.ASGITransport.
    handle_async_request) fully drain a streaming response's body before handing
    back anything at all -- neither actually streams incrementally the way a real
    socket connection (tests/integration's real Mosquitto-backed case) does. An
    EventSource backed by InMemoryEventSink._events's `while True: yield await
    queue.get()` never reaches `more_body=False`, so a request against it through
    either transport hangs forever rather than 200ing with a small buffered body.
    Route-wiring tests below use this finite stand-in instead, so the request
    actually completes; InMemoryEventSink's own publish()/subscribe() fan-out
    behavior is covered directly, with no HTTP layer at all, by
    tests/unit/crud/interfaces/test_base.py's `test_in_memory_event_sink_*` tests, and
    genuine concurrent delivery over a live SSE connection is covered by
    tests/integration/crud_1/heroes/test_heroes_v2_events.py against the real
    Mosquitto service.
    """
    for _ in ():  # pragma: no cover -- makes this a generator; the loop body never runs
        yield _


@dataclass(frozen=True)
class _FiniteEventSource:
    """EventSource whose subscription ends immediately -- see _no_events_ever's docstring."""

    async def subscribe(
        self, subscriber_id: str | None
    ) -> tuple[str, AsyncIterator[dict[str, Any]]]:
        """Resolve subscriber_id and return it with an iterator that yields nothing."""
        return subscriber_id or "stub-subscriber", _no_events_ever()


def test_json_router_events_route_exists() -> None:
    """GET <prefix>/events exists and streams text/event-stream, given the read role."""
    events_app.dependency_overrides[get_gadget_event_source] = _FiniteEventSource
    try:
        response = events_client.get("/gadgets/events", headers={"X-Role": "viewer"})
    finally:
        del events_app.dependency_overrides[get_gadget_event_source]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("id: 0")


def test_json_router_events_route_requires_the_same_role_as_get() -> None:
    """GET <prefix>/events 403s without the role GET <prefix> itself requires."""
    list_response = events_client.get("/gadgets")
    assert list_response.status_code == 403

    events_response = events_client.get("/gadgets/events")
    assert events_response.status_code == 403


# --- XML parity: clone/publish 404s, bulk restore, and /events ---------------


def test_xml_router_clone_missing_returns_404() -> None:
    """POST /gadgets/xml/clone?id=<missing> 404s, same as the JSON router's clone_record."""
    response = events_client.post(
        "/gadgets/xml/clone", params={"id": 999}, headers={"X-Role": "viewer"}
    )
    assert response.status_code == 404


def test_xml_router_publish_missing_returns_404() -> None:
    """POST /gadgets/xml/publish?id=<missing> 404s, same as the JSON router's publish_record."""
    response = events_client.post(
        "/gadgets/xml/publish", params={"id": 999}, headers={"X-Role": "viewer"}
    )
    assert response.status_code == 404


def test_xml_router_bulk_restore_via_filters_renders_bulk_update_result() -> None:
    """POST /gadgets/xml/restore with no id renders a `<bulk-update-result>` body."""
    response = events_client.post(
        "/gadgets/xml/restore",
        params={"name__icontains": "widget"},
        headers={"X-Role": "viewer"},
    )
    assert response.status_code == 200
    assert "<bulk-update-result>" in response.text
    assert "<matched>0</matched>" in response.text


def test_xml_router_events_route_exists() -> None:
    """GET /gadgets/xml/events exists and streams text/event-stream, given the read role."""
    events_app.dependency_overrides[get_gadget_event_source] = _FiniteEventSource
    try:
        response = events_client.get("/gadgets/xml/events", headers={"X-Role": "viewer"})
    finally:
        del events_app.dependency_overrides[get_gadget_event_source]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("id: 0")


def test_xml_router_stats_without_bucket_omits_time_buckets() -> None:
    """GET /gadgets/xml/stats with no `?bucket=` omits the `<time-buckets>` element entirely."""
    response = client.get("/gadgets/xml/stats")
    assert response.status_code == 200
    assert "<time-buckets>" not in response.text


# --- _sse_events: exercised directly, for deterministic control over timing/disconnect --


class _StubRequest:
    """Stand-in for fastapi.Request, controlling exactly when is_disconnected() flips True."""

    def __init__(self, *, disconnect_after_calls: int | None) -> None:
        """`disconnect_after_calls=N` means the Nth call onward reports disconnected;
        None means never.
        """
        self._calls = 0
        self._disconnect_after_calls = disconnect_after_calls

    async def is_disconnected(self) -> bool:
        """Report disconnected once this has been called `disconnect_after_calls` times."""
        self._calls += 1
        return (
            self._disconnect_after_calls is not None and self._calls > self._disconnect_after_calls
        )


async def _no_events_forthcoming() -> AsyncIterator[dict[str, Any]]:
    """An EventSource iterator that never yields -- forces _sse_events into a keep-alive wait."""
    for _ in ():  # pragma: no cover -- makes this a generator; the loop body never runs
        yield _
    await asyncio.Event().wait()


async def _one_event(event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """An EventSource iterator yielding exactly one event, then ending."""
    yield event


async def test_sse_events_yields_subscriber_id_frame_then_each_event() -> None:
    """The first frame carries subscriber_id; each subsequent event gets its own id: line."""
    request = cast(Request, _StubRequest(disconnect_after_calls=None))
    frames = [
        frame
        async for frame in crud_router_module._sse_events(
            request, "sub-1", _one_event({"resource": "gadget", "action": "create"})
        )
    ]
    assert frames == [
        'id: 0\ndata: {"subscriber_id": "sub-1"}\n\n',
        'id: 1\ndata: {"resource": "gadget", "action": "create"}\n\n',
    ]


async def test_sse_events_yields_keep_alive_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keep-alive comment is yielded once no event arrives within sse_keepalive_seconds."""
    monkeypatch.setattr(crud_router_module.settings, "sse_keepalive_seconds", 0.01)
    request = cast(Request, _StubRequest(disconnect_after_calls=1))  # disconnect after keep-alive
    events = _no_events_forthcoming()
    frames = [frame async for frame in crud_router_module._sse_events(request, "sub-1", events)]
    assert frames == ['id: 0\ndata: {"subscriber_id": "sub-1"}\n\n', ": keep-alive\n\n"]


async def test_sse_events_reuses_the_same_pending_task_across_keep_alives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second keep-alive still waits on the *same* pending `__anext__()` task rather
    than starting a new one -- see _sse_events's own docstring for why a fresh one per
    keep-alive would silently end the stream instead.
    """
    monkeypatch.setattr(crud_router_module.settings, "sse_keepalive_seconds", 0.01)
    request = cast(Request, _StubRequest(disconnect_after_calls=2))  # two keep-alives, then gone
    events = _no_events_forthcoming()
    frames = [frame async for frame in crud_router_module._sse_events(request, "sub-1", events)]
    assert frames == [
        'id: 0\ndata: {"subscriber_id": "sub-1"}\n\n',
        ": keep-alive\n\n",
        ": keep-alive\n\n",
    ]


async def test_sse_events_ends_on_client_disconnect() -> None:
    """The stream ends as soon as request.is_disconnected() reports the client is gone."""
    request = cast(Request, _StubRequest(disconnect_after_calls=0))  # disconnected immediately
    events = _no_events_forthcoming()
    frames = [frame async for frame in crud_router_module._sse_events(request, "sub-1", events)]
    assert frames == ['id: 0\ndata: {"subscriber_id": "sub-1"}\n\n']


def test_json_router_filters_metadata_describes_every_filterable_field() -> None:
    """GET /gadgets/filters describes every filterable field's kind and ops."""
    response = client.get("/gadgets/filters")
    assert response.status_code == 200
    by_name = {info["name"]: info for info in response.json()}
    assert "tags" not in by_name  # list fields aren't filterable
    assert by_name["name"]["kind"] == "string"
    assert set(by_name["name"]["ops"]) == {"eq", "contains", "icontains", "regex"}
    assert by_name["id"]["kind"] == "number"


def test_web_component_bulk_ui_reaches_the_json_router() -> None:
    """The rendered web component's bulk-delete call (id__in=) works against the JSON router.

    The web router itself has no data routes -- its JS fetches directly against
    api_base (the sibling JSON router's own prefix), so this exercises that the
    two routers' contracts actually line up end to end, not just that each
    factory's routes work in isolation.
    """
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        first = client.post("/gadgets", json={"name": "apple", "tags": []}).json()
        second = client.post("/gadgets", json={"name": "apricot", "tags": []}).json()
        client.post("/gadgets", json={"name": "banana", "tags": []})

        response = client.delete("/gadgets", params={"id__in": f"{first['id']},{second['id']}"})
        assert response.status_code == 200
        assert response.json()["matched"] == 2

        remaining = client.get("/gadgets").json()
        assert [g["name"] for g in remaining] == ["banana"]
    finally:
        del app.dependency_overrides[get_gadget_crud]


# --- `/stats`/`/predict`: opt-in via stats_enabled ----------------------------


def test_json_router_stats_reports_total_and_numeric_field() -> None:
    """GET /gadgets/stats reports the total count and the numeric ("id") field's stats."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        client.post("/gadgets", json={"name": "banana", "tags": []})
        response = client.get("/gadgets/stats")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["time_series"] is None
        assert body["lifecycle"] is None
        numeric_by_field = {item["field"]: item for item in body["numeric"]}
        assert numeric_by_field["id"]["count"] == 2
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_stats_with_bucket_returns_time_series() -> None:
    """GET /gadgets/stats?bucket=day includes a time-bucketed count series."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        first_id = client.post("/gadgets", json={"name": "apple", "tags": []}).json()["id"]
        second_id = client.post("/gadgets", json={"name": "banana", "tags": []}).json()["id"]
        repository._records[first_id].bucket = "2024-01-01T00:00:00"
        repository._records[second_id].bucket = "2024-01-02T00:00:00"
        response = client.get("/gadgets/stats", params={"bucket": "day"})
        assert response.status_code == 200
        body = response.json()
        assert len(body["time_series"]) == 2
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_json_router_stats_rejects_invalid_bucket() -> None:
    """GET /gadgets/stats?bucket=<invalid> is rejected (422), not a 500."""
    response = client.get("/gadgets/stats", params={"bucket": "fortnight"})
    assert response.status_code == 422


def test_json_router_predict_rejects_unrecognized_field() -> None:
    """GET /gadgets/predict?field=<non-numeric> is rejected (422)."""
    response = client.get("/gadgets/predict", params={"field": "name"})
    assert response.status_code == 422


def test_json_router_predict_rejects_insufficient_history() -> None:
    """GET /gadgets/predict with fewer than 2 time buckets of history is rejected (422)."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        client.post("/gadgets", json={"name": "apple", "tags": []})
        response = client.get("/gadgets/predict")
    finally:
        del app.dependency_overrides[get_gadget_crud]
    assert response.status_code == 422


def test_json_router_predict_projects_future_buckets() -> None:
    """GET /gadgets/predict forecasts `periods` future buckets from a real time series."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        for day, name in enumerate(["apple", "banana", "cherry"], start=1):
            gadget_id = client.post("/gadgets", json={"name": name, "tags": []}).json()["id"]
            repository._records[gadget_id].bucket = f"2024-01-0{day}T00:00:00"
        response = client.get("/gadgets/predict", params={"periods": 3, "bucket": "day"})
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "linear_regression"
        assert body["field"] is None
        assert len(body["predictions"]) == 3
    finally:
        del app.dependency_overrides[get_gadget_crud]


def test_xml_router_stats_and_predict_render_nested_xml() -> None:
    """GET /gadgets/xml/stats and /predict hand-assemble the same data as nested XML."""
    repository = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repository
    )
    try:
        for day, name in enumerate(["apple", "banana"], start=1):
            gadget_id = client.post("/gadgets", json={"name": name, "tags": []}).json()["id"]
            repository._records[gadget_id].bucket = f"2024-01-0{day}T00:00:00"

        stats_response = client.get("/gadgets/xml/stats", params={"bucket": "day"})
        assert stats_response.status_code == 200
        assert stats_response.headers["content-type"] == "application/xml"
        assert "<stats>" in stats_response.text
        assert "<total>2</total>" in stats_response.text
        assert "<time-buckets>" in stats_response.text

        predict_response = client.get(
            "/gadgets/xml/predict", params={"periods": 1, "bucket": "day"}
        )
        assert predict_response.status_code == 200
        assert predict_response.headers["content-type"] == "application/xml"
        assert "<prediction>" in predict_response.text
        assert "<meta>" in predict_response.text
        assert predict_response.text.count("<prediction-point>") == 1
    finally:
        del app.dependency_overrides[get_gadget_crud]
