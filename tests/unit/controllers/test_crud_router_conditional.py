"""Unit test: ETag / 304 / If-Match / 412 on the JSON and XML router factories (Phase 6.1)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel
from starlette.requests import Request

from craftainer_crud.controllers.crud_router import _expected_version, list_etag, record_etag
from craftainer_crud.interfaces.base import CRUDInterface
from tests.unit.controllers.test_crud_router import (
    _FakeGadgetRepository,
    _Gadget,
    app,
    client,
    get_gadget_crud,
)

REVALIDATE = "private, no-cache"
FORMATS = pytest.mark.parametrize("base", ["/gadgets", "/gadgets/xml"])


@pytest.fixture(autouse=True)
def _repository() -> Iterator[_FakeGadgetRepository]:
    """Wire a fresh repository into the shared gadget app for each test."""
    repo = _FakeGadgetRepository()
    app.dependency_overrides[get_gadget_crud] = lambda: CRUDInterface(
        schema=_Gadget, repository=repo
    )
    yield repo
    del app.dependency_overrides[get_gadget_crud]


def _create(name: str = "Widget") -> int:
    response = client.post("/gadgets", json={"name": name, "tags": ["a"]})
    return int(response.json()["id"])


class _Stamped(BaseModel):
    id: int
    updated_at: datetime


def test_record_etag_uses_id_and_updated_at() -> None:
    """Same id + updated_at gives the same strong tag; either changing gives another."""
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    base = record_etag(_Stamped(id=1, updated_at=stamp))
    assert base == record_etag(_Stamped(id=1, updated_at=stamp))
    assert base.startswith('"')
    assert base != record_etag(_Stamped(id=2, updated_at=stamp))
    assert base != record_etag(_Stamped(id=1, updated_at=datetime(2026, 1, 2, tzinfo=UTC)))


def test_list_etag_is_weak_and_follows_the_page() -> None:
    """A list tag is weak, and changes when a member's updated_at does."""
    first = _Stamped(id=1, updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = _Stamped(id=1, updated_at=datetime(2026, 1, 2, tzinfo=UTC))
    assert list_etag([first]).startswith('W/"')
    assert list_etag([first]) != list_etag([second])
    assert list_etag([]) == list_etag([])


@FORMATS
def test_get_sends_etag_and_cache_control(base: str) -> None:
    """Single and list GETs carry an ETag (strong / weak) and `private, no-cache`."""
    gadget_id = _create()
    single = client.get(base, params={"id": gadget_id})
    listing = client.get(base)
    assert single.status_code == listing.status_code == 200
    assert single.headers["etag"].startswith('"')
    assert listing.headers["etag"].startswith('W/"')
    assert single.headers["cache-control"] == listing.headers["cache-control"] == REVALIDATE


@FORMATS
def test_if_none_match_returns_bodiless_304(base: str) -> None:
    """A matching If-None-Match (strong, weak-prefixed, listed or `*`) gets 304 and no body."""
    gadget_id = _create()
    single = client.get(base, params={"id": gadget_id})
    listing = client.get(base)
    for url_params, response in (({"id": gadget_id}, single), ({}, listing)):
        etag = response.headers["etag"]
        for header in (etag, f"W/{etag.removeprefix('W/')}", f'"other", {etag}', "*"):
            again = client.get(base, params=url_params, headers={"If-None-Match": header})
            assert again.status_code == 304
            assert again.content == b""
            assert again.headers["etag"] == etag
            assert again.headers["cache-control"] == REVALIDATE


@FORMATS
def test_stale_if_none_match_returns_full_response(base: str) -> None:
    """A non-matching If-None-Match, or one for a changed record, still gets the 200 body."""
    gadget_id = _create()
    old = client.get(base, params={"id": gadget_id}).headers["etag"]
    other = client.get(base, params={"id": gadget_id}, headers={"If-None-Match": '"nope"'})
    assert other.status_code == 200
    client.patch("/gadgets", params={"id": gadget_id}, json={"tags": ["changed"]})
    fresh = client.get(base, params={"id": gadget_id}, headers={"If-None-Match": old})
    assert fresh.status_code == 200
    assert fresh.headers["etag"] != old


@FORMATS
def test_patch_if_match_applies_or_fails_with_412(base: str) -> None:
    """PATCH succeeds with the current tag (returning the new one) and 412s on a stale one."""
    gadget_id = _create()
    etag = client.get("/gadgets", params={"id": gadget_id}).headers["etag"]
    body = (
        {"json": {"tags": ["b"]}}
        if base == "/gadgets"
        else {"content": b"<gadget><tags>b</tags></gadget>"}
    )
    ok = client.patch(base, params={"id": gadget_id}, headers={"If-Match": etag}, **body)
    assert ok.status_code == 200
    new_etag = ok.headers["etag"]
    assert new_etag != etag
    assert new_etag == client.get("/gadgets", params={"id": gadget_id}).headers["etag"]
    stale = client.patch(base, params={"id": gadget_id}, headers={"If-Match": etag}, **body)
    assert stale.status_code == 412
    assert stale.headers["content-type"].startswith("application/json")
    assert "modified" in stale.json()["detail"]


@FORMATS
def test_delete_if_match_applies_or_fails_with_412(base: str) -> None:
    """DELETE 412s on a stale/weak tag and succeeds with the current one or `*`."""
    first, second = _create("one"), _create("two")
    etag = client.get("/gadgets", params={"id": first}).headers["etag"]
    for bad in ('"stale"', f"W/{etag}"):
        refused = client.delete(base, params={"id": first}, headers={"If-Match": bad})
        assert refused.status_code == 412
    assert client.delete(base, params={"id": first}, headers={"If-Match": etag}).status_code in (
        200,
        204,
    )
    assert client.delete(base, params={"id": second}, headers={"If-Match": "*"}).status_code in (
        200,
        204,
    )


def test_if_match_on_a_missing_record_falls_through_to_404() -> None:
    """There's nothing to compare against, so the write's own 404 wins."""
    response = client.patch("/gadgets", params={"id": 99}, headers={"If-Match": '"x"'}, json={})
    assert response.status_code == 404


def test_if_match_is_ignored_for_bulk_actions() -> None:
    """A bulk action has no single representation, so If-Match doesn't gate it."""
    _create()
    response = client.patch(
        "/gadgets", params={"name": "Widget"}, headers={"If-Match": '"x"'}, json={"tags": ["z"]}
    )
    assert response.status_code == 200
    assert "etag" not in response.headers
    removed = client.delete("/gadgets", params={"name": "Widget"}, headers={"If-Match": '"x"'})
    assert removed.status_code == 200
    assert "etag" not in removed.headers


@FORMATS
def test_matched_if_match_passes_the_records_version_to_the_write(
    base: str, _repository: _FakeGadgetRepository
) -> None:
    """A matching If-Match becomes `expected_version` (the record's updated_at) on the write."""
    first, second = _create("one"), _create("two")
    etag = client.get("/gadgets", params={"id": first}).headers["etag"]
    version = _repository._records[first].updated_at
    body = (
        {"json": {"tags": ["b"]}}
        if base == "/gadgets"
        else {"content": b"<gadget><tags>b</tags></gadget>"}
    )
    assert client.patch(base, params={"id": first}, headers={"If-Match": etag}, **body).is_success
    assert _repository.writes[-1] == ("update", version)
    etag = client.get("/gadgets", params={"id": second}).headers["etag"]
    version = _repository._records[second].updated_at
    assert client.delete(base, params={"id": second}, headers={"If-Match": etag}).is_success
    assert _repository.writes[-1] == ("delete", version)


@FORMATS
def test_unconditional_requests_pass_no_expected_version(
    base: str, _repository: _FakeGadgetRepository
) -> None:
    """No If-Match, or `If-Match: *`, writes unconditionally (expected_version=None)."""
    first, second = _create("one"), _create("two")
    body = (
        {"json": {"tags": ["b"]}}
        if base == "/gadgets"
        else {"content": b"<gadget><tags>b</tags></gadget>"}
    )
    assert client.patch(base, params={"id": first}, **body).is_success
    assert _repository.writes[-1] == ("update", None)
    assert client.patch(base, params={"id": first}, headers={"If-Match": "*"}, **body).is_success
    assert _repository.writes[-1] == ("update", None)
    assert client.delete(base, params={"id": second}, headers={"If-Match": "*"}).is_success
    assert _repository.writes[-1] == ("delete", None)


@FORMATS
def test_a_writer_slipping_in_after_the_comparison_still_gets_412(
    base: str, _repository: _FakeGadgetRepository
) -> None:
    """The version is re-checked atomically by the write: the same 412 as a stale If-Match."""
    gadget_id = _create()
    etag = client.get("/gadgets", params={"id": gadget_id}).headers["etag"]
    body = (
        {"json": {"tags": ["b"]}}
        if base == "/gadgets"
        else {"content": b"<gadget><tags>b</tags></gadget>"}
    )
    _repository.concurrent_write_after_get = True
    patched = client.patch(base, params={"id": gadget_id}, headers={"If-Match": etag}, **body)
    _repository.concurrent_write_after_get = False
    etag = client.get("/gadgets", params={"id": gadget_id}).headers["etag"]
    _repository.concurrent_write_after_get = True
    deleted = client.delete(base, params={"id": gadget_id}, headers={"If-Match": etag})
    assert patched.status_code == deleted.status_code == 412
    assert (
        patched.json()["detail"]
        == deleted.json()["detail"]
        == ("Gadget was modified since the supplied If-Match")
    )
    assert gadget_id in _repository._records


class _Plain(BaseModel):
    id: int


class _OneRecordCrud:
    """Just enough of CRUDLike for `_expected_version`: `get` returns a fixed view."""

    def __init__(self, record: BaseModel) -> None:
        self._record = record

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> BaseModel:
        return self._record


def _request(if_match: str) -> Request:
    return Request(
        {"type": "http", "headers": [(b"if-match", if_match.encode())], "query_string": b""}
    )


async def test_expected_version_is_none_for_a_view_without_updated_at() -> None:
    """Such a view still gets the pre-write comparison, but there is no version to pass on."""
    record = _Plain(id=1)
    crud = _OneRecordCrud(record)
    assert await _expected_version(crud, _request(record_etag(record)), id=1, not_found="x") is None


async def test_expected_version_is_normalised_to_naive_utc() -> None:
    """A view carrying an offset is converted to the naive-UTC value the columns hold."""
    aware = _Stamped(id=1, updated_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    got = await _expected_version(
        _OneRecordCrud(aware), _request(record_etag(aware)), id=1, not_found="x"
    )
    assert got == datetime(2026, 1, 1, 12, 0)
    assert got is not None
    assert got.tzinfo is None
