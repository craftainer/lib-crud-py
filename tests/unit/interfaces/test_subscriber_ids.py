"""Unit test: server-minted, HMAC-signed, resource-scoped subscriber ids."""

import gc
import hashlib
import hmac

import pytest

from craftainer_crud.config import MIN_SUBSCRIBER_SECRET_LEN, CrudConfig
from craftainer_crud.interfaces.base import InMemoryEventSink
from craftainer_crud.interfaces.subscriber_ids import (
    mint_subscriber_id,
    resolve_subscriber_id,
    verify_subscriber_id,
)
from tests.unit.conftest import CrudConfigOverride

_SECRET = b"s" * 32


def test_minted_ids_are_64_hex_characters_and_unique() -> None:
    """An id is 128 nonce bits plus a 128-bit tag, hex, and never repeats."""
    ids = {mint_subscriber_id("hero") for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 64 and set(i) <= set("0123456789abcdef") for i in ids)


def test_a_minted_id_verifies_for_its_resource_only() -> None:
    """The tag covers the resource, so an id from another resource is refused."""
    subscriber_id = mint_subscriber_id("hero")
    assert verify_subscriber_id("hero", subscriber_id) == subscriber_id
    assert verify_subscriber_id("hero", f"  {subscriber_id}\n") == subscriber_id
    assert verify_subscriber_id("quest", subscriber_id) is None


@pytest.mark.parametrize(
    "forged",
    [
        None,
        "",
        "abc",
        "known-id",
        "x" * 64,
        "0" * 64,
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "../" * 21 + "a",
    ],
)
def test_a_guessed_or_malformed_id_does_not_verify(forged: str | None) -> None:
    """Anything the server did not mint is no id at all."""
    assert verify_subscriber_id("hero", forged) is None


def test_a_tampered_id_does_not_verify() -> None:
    """Flipping one character of the nonce or the tag breaks the signature."""
    subscriber_id = mint_subscriber_id("hero")
    for index in (0, 31, 32, 63):
        flipped = "0" if subscriber_id[index] != "0" else "1"
        tampered = subscriber_id[:index] + flipped + subscriber_id[index + 1 :]
        assert verify_subscriber_id("hero", tampered) is None


def test_resolve_keeps_a_valid_id_and_mints_over_an_invalid_one() -> None:
    """A valid id resumes; a forged one is replaced by a fresh server-minted id."""
    subscriber_id = mint_subscriber_id("hero")
    assert resolve_subscriber_id("hero", subscriber_id) == subscriber_id
    replaced = resolve_subscriber_id("hero", "known-id")
    assert replaced != "known-id"
    assert verify_subscriber_id("hero", replaced) == replaced


def test_ids_follow_the_configured_secret(override_crud_config: CrudConfigOverride) -> None:
    """An id minted under one secret verifies under it again, and not under another."""
    override_crud_config(subscriber_secret=_SECRET)
    subscriber_id = mint_subscriber_id("hero")
    assert verify_subscriber_id("hero", subscriber_id) == subscriber_id
    override_crud_config(subscriber_secret=b"t" * 32)
    assert verify_subscriber_id("hero", subscriber_id) is None
    override_crud_config(subscriber_secret=None)
    assert verify_subscriber_id("hero", subscriber_id) is None


def test_matches_the_documented_construction(override_crud_config: CrudConfigOverride) -> None:
    """tag = HMAC-SHA256(secret, resource + NUL + nonce_hex)[:16], hex (the Rust crate's too)."""
    override_crud_config(subscriber_secret=_SECRET)
    nonce = "00112233445566778899aabbccddeeff"
    expected = hmac.new(_SECRET, b"hero\x00" + nonce.encode(), hashlib.sha256).hexdigest()[:32]
    assert verify_subscriber_id("hero", nonce + expected) == nonce + expected


def test_config_rejects_a_short_subscriber_secret() -> None:
    """A secret under 32 bytes is refused when the config is built."""
    with pytest.raises(ValueError, match="at least 32 bytes"):
        CrudConfig(subscriber_secret=b"short")
    with pytest.raises(TypeError, match="must be bytes"):
        CrudConfig(subscriber_secret="s" * 40)  # type: ignore[arg-type]
    assert CrudConfig(subscriber_secret=_SECRET).subscriber_secret == _SECRET
    assert MIN_SUBSCRIBER_SECRET_LEN == 32


async def test_in_memory_sink_will_not_resume_a_forged_id() -> None:
    """A guessed id gets a new session; the live session under another id is untouched."""
    sink = InMemoryEventSink(resource="hero")
    live_id, live_events = await sink.subscribe(None)
    guessed, _ = await sink.subscribe("known-id")
    assert guessed != "known-id"
    assert set(sink._queues) == {live_id, guessed}

    await sink.publish(resource="hero", record_id=1, action="create", snapshot={})
    assert (await live_events.__anext__())["record_id"] == 1


async def test_in_memory_sink_resumes_its_own_id_and_scopes_ids_per_resource() -> None:
    """A minted id is echoed back on resume, but not accepted by another resource's sink."""
    hero = InMemoryEventSink(resource="hero")
    quest = InMemoryEventSink(resource="quest")
    subscriber_id, _ = await hero.subscribe(None)
    assert (await hero.subscribe(subscriber_id))[0] == subscriber_id
    assert (await quest.subscribe(subscriber_id))[0] != subscriber_id


async def test_in_memory_sink_aclose_before_iterating_drops_the_queue() -> None:
    """An iterator closed without being iterated no longer holds its queue."""
    sink = InMemoryEventSink(resource="hero")
    _, events = await sink.subscribe(None)
    assert len(sink._queues) == 1
    await events.aclose()  # type: ignore[attr-defined]
    assert sink._queues == {}


async def test_in_memory_sink_drops_the_queue_of_a_garbage_collected_iterator() -> None:
    """Dropping an unstarted iterator releases its queue."""
    sink = InMemoryEventSink(resource="hero")
    _, events = await sink.subscribe(None)
    del events
    gc.collect()
    assert sink._queues == {}


async def test_in_memory_sink_release_leaves_a_re_registered_queue_alone() -> None:
    """Closing a stale iterator does not remove the queue a resumed id now uses."""
    sink = InMemoryEventSink(resource="hero")
    subscriber_id, old = await sink.subscribe(None)
    _, new = await sink.subscribe(subscriber_id)
    await old.aclose()  # type: ignore[attr-defined]
    assert list(sink._queues) == [subscriber_id]
    await new.aclose()  # type: ignore[attr-defined]
    assert sink._queues == {}
