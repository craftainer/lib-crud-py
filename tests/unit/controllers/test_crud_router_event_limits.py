"""Unit test: the `/events` per-caller stream cap, transient-by-default sessions, and keys."""

import asyncio
import gc
from collections.abc import AsyncGenerator, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request

from craftainer_crud.controllers.crud_router import _EventCaller, _open_event_stream
from craftainer_crud.controllers.stream_limits import (
    StreamLimiter,
    caller_key,
    stream_limiter,
)
from craftainer_crud.interfaces.base import EventIterator, EventSource, OwnerScope
from tests.unit.conftest import CrudConfigOverride
from tests.unit.controllers.test_crud_router import _scoped_events_client, events_client


@pytest.fixture(autouse=True)
def _clean_limiter() -> Iterator[None]:
    stream_limiter._counts.clear()
    yield
    gc.collect()  # finalizers of dropped responses release into the limiter first
    stream_limiter._counts.clear()


class _Source:
    """EventSource recording how it was subscribed; its iterator never ends on its own."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str | None, bool]] = []
        self.fail = fail

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        self.calls.append((subscriber_id, durable))
        if self.fail:
            msg = "broker down"
            raise OSError(msg)

        async def forever() -> AsyncGenerator[dict[str, Any]]:
            await asyncio.Event().wait()
            yield {}

        return subscriber_id or "fresh", forever()


def _request(**headers: str) -> Request:
    async def is_disconnected() -> bool:
        return False

    return cast(Request, SimpleNamespace(headers=headers, is_disconnected=is_disconnected))


def _caller(key: object = "k") -> _EventCaller:
    return _EventCaller(lambda _event: True, key)


async def _open(source: _Source, caller: _EventCaller, **kwargs: Any) -> Any:  # noqa: ANN401
    return await _open_event_stream(
        _request(**kwargs.pop("headers", {})),
        cast(EventSource, source),
        caller,
        subscriber_id=kwargs.pop("subscriber_id", None),
        resource="gadget",
    )


async def test_streams_are_transient_by_default_and_ignore_a_resume_id() -> None:
    """A clean-session connect under a durable id would discard it, so the id is not passed."""
    source = _Source()
    response = await _open(source, _caller(), subscriber_id="abc", headers={"last-event-id": "x"})
    assert source.calls == [(None, False)]
    await response.body_iterator.aclose()


async def test_durable_sessions_opt_in_resumes_by_query_or_header(
    override_crud_config: CrudConfigOverride,
) -> None:
    """With sse_durable_sessions the id (query first, then Last-Event-ID) is resumed."""
    override_crud_config(sse_durable_sessions=True)
    source = _Source()
    first = await _open(source, _caller(), subscriber_id="abc")
    second = await _open(source, _caller(), headers={"last-event-id": "hdr"})
    assert source.calls == [("abc", True), ("hdr", True)]
    await first.body_iterator.aclose()
    await second.body_iterator.aclose()


async def test_the_cap_refuses_with_429_and_frees_a_slot_when_a_stream_ends(
    override_crud_config: CrudConfigOverride,
) -> None:
    """The sixth stream of five is a 429; closing one admits the next; others are unaffected."""
    override_crud_config(sse_max_streams_per_caller=2)
    source = _Source()
    first = await _open(source, _caller("a"))
    second = await _open(source, _caller("a"))
    with pytest.raises(HTTPException) as refused:
        await _open(source, _caller("a"))
    assert refused.value.status_code == 429
    other = await _open(source, _caller("b"))
    await anext(first.body_iterator)
    await first.body_iterator.aclose()
    third = await _open(source, _caller("a"))
    assert second is not third  # both held open until here
    await other.body_iterator.aclose()


async def test_the_default_cap_is_five() -> None:
    """Without configuration a caller holds five streams and the sixth is refused."""
    source = _Source()
    held = [await _open(source, _caller()) for _ in range(5)]
    with pytest.raises(HTTPException, match="too many"):
        await _open(source, _caller())
    assert len(held) == 5


async def test_no_cap_when_unset(override_crud_config: CrudConfigOverride) -> None:
    """sse_max_streams_per_caller=None removes the cap."""
    override_crud_config(sse_max_streams_per_caller=None)
    source = _Source()
    held = [await _open(source, _caller()) for _ in range(8)]
    assert stream_limiter.open_streams("k") == len(held) == 8


async def test_a_failed_subscribe_gives_the_slot_back() -> None:
    """A broker failure while subscribing must not leak the caller's slot."""
    with pytest.raises(OSError, match="broker down"):
        await _open(_Source(fail=True), _caller())
    assert stream_limiter.open_streams("k") == 0


async def test_a_response_dropped_before_streaming_gives_the_slot_back() -> None:
    """The generator's own finally cannot run if it never started; the response finalizer does."""
    response = await _open(_Source(), _caller())
    assert stream_limiter.open_streams("k") == 1
    del response
    gc.collect()
    assert stream_limiter.open_streams("k") == 0


def test_caller_key_uses_the_owner_value_across_resources_and_one_bucket_when_unscoped() -> None:
    """Owner scopes key on the value; unscoped callers share one bucket per resource."""
    assert caller_key(OwnerScope("owner", "alice"), "a") == caller_key(
        OwnerScope("owner", "alice"), "b"
    )
    assert caller_key(OwnerScope("owner", "alice"), "a") != caller_key(
        OwnerScope("owner", "bob"), "a"
    )
    assert caller_key(None, "a") != caller_key(None, "b")
    assert caller_key(None, "a") != caller_key(OwnerScope("owner", "a"), "a")


def test_limiter_release_is_idempotent() -> None:
    """Releasing a lease twice does not free a second slot."""
    limiter = StreamLimiter()
    first = limiter.acquire("k", 2)
    second = limiter.acquire("k", 2)
    assert first is not None
    assert second is not None
    first.release()
    first.release()
    assert limiter.open_streams("k") == 1
    assert limiter.acquire("k", 1) is None


@pytest.mark.parametrize("path", ["/json/events", "/xml/events"])
@pytest.mark.parametrize("with_scope_dependency", [True, False])
def test_events_over_the_cap_for_the_owner_is_429(
    path: str, with_scope_dependency: bool, override_crud_config: CrudConfigOverride
) -> None:
    """The owner scope (from the scope dependency or the CRUD's own) keys the cap."""
    override_crud_config(sse_max_streams_per_caller=1)
    client, _ = _scoped_events_client(with_scope_dependency=with_scope_dependency)
    held = stream_limiter.acquire(caller_key(OwnerScope("owner", "alice"), ""), 1)
    assert held is not None
    assert client.get(path).status_code == 429
    held.release()
    assert client.get(path).status_code == 200


def test_events_without_an_owner_scope_share_one_bucket_per_resource(
    override_crud_config: CrudConfigOverride,
) -> None:
    """An unscoped resource is capped as a whole, keyed on its label."""
    override_crud_config(sse_max_streams_per_caller=1)
    held = stream_limiter.acquire(caller_key(None, "Gadget"), 1)
    assert held is not None
    response = events_client.get("/gadgets/events", headers={"X-Role": "viewer"})
    assert response.status_code == 429
    held.release()
