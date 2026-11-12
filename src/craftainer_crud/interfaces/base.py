"""Generic CRUD interface.

Feed it a Pydantic view (app.views/craftainer_crud.views) and a Repository
(craftainer_crud.repositories) to persist
it through, and it exposes CRUD operations that speak entirely in terms of that
view -- converting to and from the backing ORM model via the view's own
`from_attributes` support (see craftainer_crud.views.base.ORMView) is the only place that
conversion happens, so a new resource never needs its own CRUD class.

A few branches below are `# pragma: no cover` for the same reason as
craftainer_crud.repositories.sqlalchemy's module docstring: Hero -- the only resource
tests/e2e's journeys exercise -- always builds its CRUDInterface with `owner`,
`revisions`, and `events` all set (see app.crud_1.heroes.heroes_v2.get_hero_crud),
and `owner.read_scoped=False`, so the `owner is None`/`revisions is None`/
`events is None`/`owner.read_scoped is True` branches below can never run
through `tests/e2e`. tests/unit/interfaces/test_base.py exercises every one of
them directly against a standalone owner-less/revision-less/event-less
CRUDInterface, which is what actually covers them for the primary (`pytest`,
i.e. tests/unit + tests/integration) coverage gate; the pragma only affects
what's counted toward the separate `pytest tests/e2e` coverage gate.
"""

import asyncio
import json
import logging
import ssl
import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel

from craftainer_crud.config import (
    DEFAULT_SESSION_TTL_SECONDS,
    build_mqtt_tls_context,
    validate_mqtt_security,
)
from craftainer_crud.interfaces.delivery import deliver_event, drain_event_deliveries
from craftainer_crud.interfaces.subscriber_ids import resolve_subscriber_id
from craftainer_crud.repositories.base import Repository
from craftainer_crud.repositories.filtering import FilterClause, FilterOp, SortClause
from craftainer_crud.repositories.stats import ResourceStats, TimeBucket

if TYPE_CHECKING:
    import aiomqtt  # only imported at runtime inside the MQTT classes (future `[mqtt]` extra)

logger = logging.getLogger(__name__)


class RevisionSink(Protocol):
    """Opt-in append-only revision-history hook for a CRUDInterface.

    A small Protocol (rather than a concrete class) so craftainer_crud.interfaces stays
    unaware of craftainer_crud.models.revision.Revision's own storage shape -- see
    RepositoryRevisionSink below for the concrete adapter every resource that
    opts in actually uses.
    """

    async def record(
        self, *, resource: str, record_id: int, action: str, snapshot: dict[str, Any], actor: str
    ) -> None:
        """Append one revision log entry."""
        ...


@dataclass(frozen=True)
class RepositoryRevisionSink:
    """RevisionSink backed by a plain Repository[Revision] -- no bespoke storage class needed.

    `craftainer_crud.models.revision.Revision` is just another IdentifiedBase model, so the
    same `Repository[ModelT]`/`build_repository_provider` machinery every other
    resource uses already knows how to persist it (SQLAlchemy-backed in dev/
    production, in-memory under MODE=mock) -- this is a thin adapter from
    RevisionSink's `record(...)` call shape to `Repository.create(...)`'s dict shape.
    """

    repository: Repository[Any]

    async def record(
        self, *, resource: str, record_id: int, action: str, snapshot: dict[str, Any], actor: str
    ) -> None:
        """Persist one revision row via the wrapped repository's create()."""
        await self.repository.create(
            {
                "resource": resource,
                "record_id": record_id,
                "action": action,
                "snapshot": snapshot,
                "actor": actor,
            }
        )


class EventSink(Protocol):
    """Opt-in hook for a CRUDInterface to publish record-mutation events for real-time streaming.

    A small Protocol (rather than a concrete class), the same shape as RevisionSink
    above -- see MQTTEventSink/InMemoryEventSink below for the concrete adapters a
    resource that opts in actually uses, and EventSource (below) for the paired
    subscribe-side Protocol craftainer_crud.controllers.crud_router's `GET <prefix>/events`
    route depends on. Deliberately broader in scope than RevisionSink: fired after
    every successful create/update/update_many/delete/delete_many **and**
    restore/restore_many, since a subscriber watching a resource's real-time
    activity cares about visibility changes (restore) too -- unlike revision
    logging, which docs/adrs/0015-mqtt-for-crud-events.md and RepositoryRevisionSink's
    own docstring above deliberately scope to the original five mutating methods
    only. See docs/adrs/0015-mqtt-for-crud-events.md for why MQTT (not this app's
    existing Redis/Valkey service) backs the concrete adapter.
    """

    async def publish(
        self,
        *,
        resource: str,
        record_id: int,
        action: str,
        snapshot: dict[str, Any],
        owner: object = None,
    ) -> None:
        """Publish one event for `resource`/`record_id` -- delivery semantics are the
        concrete adapter's own (see MQTTEventSink: at-least-once at QoS 1; InMemoryEventSink:
        best-effort, no delivery guarantee).

        `owner` is the record's owner-scope value; the adapter carries it in the
        envelope's `owner` field so `GET <prefix>/events` can filter per subscriber.
        `CRUDInterface` passes it only when it has an `OwnerScope`, so a custom sink
        for an owner-less resource may still omit the parameter.
        """
        ...


class EventIterator[T](AsyncIterator[T], Protocol):
    """The async iterator `EventSource.subscribe` returns: an `AsyncIterator` you can `aclose()`.

    `subscribe()` acquires its resources before returning, so the caller must be able to
    release them even if it never iterates: call `aclose()` (for example with
    `contextlib.aclosing`). Any async generator satisfies this protocol structurally,
    because generators have `aclose()`.
    """

    async def aclose(self) -> None:
        """Release what the subscription holds; safe to call more than once."""
        ...


class EventSource(Protocol):
    """Opt-in subscribe-side counterpart to EventSink, for `GET <prefix>/events`.

    A small Protocol, structurally distinct from EventSink even though a resource's
    two concrete adapters (MQTTEventSink/MQTTEventSource, or the single
    InMemoryEventSink instance satisfying both) are typically built together and
    share the same underlying transport -- see craftainer_crud.interfaces.dependency.
    build_event_sink_provider/build_event_source_provider.
    """

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        """Resolve `subscriber_id` (issuing a new one if None, or if it is not an id this
        server minted for this resource -- see craftainer_crud.interfaces.subscriber_ids)
        and return it alongside
        an async iterator of this resource's event envelopes (the same JSON-shaped dict
        `EventSink.publish` was called with, plus a `timestamp`) from this point on.

        For the MQTT-backed adapter, a persistent subscriber_id (QoS 1, `clean_session=
        False`) is what lets a reconnecting caller receive events published while it was
        briefly disconnected -- see docs/adrs/0015-mqtt-for-crud-events.md's "Delivery
        guarantee" section. A caller that discards its subscriber_id and passes None gets
        a fresh session with no replay, by design.

        `durable=False` makes the subscription transient: the MQTT-backed adapter connects
        with a clean session, so the broker drops the session (and anything it queued) when
        the connection ends, whether it closes cleanly or crashes, and nothing is replayed
        on reconnect. Use it for a long-lived in-process consumer that always resubscribes
        from scratch (a cache invalidator) so each restart does not orphan a session.
        `GET <prefix>/events` never exposes this: its clients rely on replay.

        Resources (an MQTT client and its network loop, an in-memory queue) are acquired
        *here*, before the iterator's first step, so the caller owns them from the moment
        this returns: `aclose()` the iterator when done with it, started or not (for
        example with `contextlib.aclosing`, as for any async generator, PEP 525/533).
        `aclose()` and garbage collection are the only things the library guarantees
        release them; a task cancelled between `subscribe()` returning and the first step
        leaks them unless the caller closes the iterator in a `finally`.
        """
        ...


def wire_owner(value: object) -> str | int | float | bool | None:
    """Return the JSON-safe form of an owner value, as carried in the event envelope.

    Strings, numbers and booleans pass through unchanged; anything else (a UUID, say)
    becomes its `str()`, so the publisher and the per-subscriber filter
    (`event_visible`) compare the same representation on both sides of a broker.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _event_envelope(
    *, resource: str, record_id: int, action: str, snapshot: dict[str, Any], owner: object = None
) -> dict[str, Any]:
    """Build the JSON-safe event envelope shared by every EventSink/EventSource adapter.

    `owner` is the owning scope's value for the record (`OwnerScope.value`), or None
    for a resource without an owner scope; `event_visible` filters on it.
    """
    return {
        "resource": resource,
        "record_id": record_id,
        "action": action,
        "snapshot": snapshot,
        "owner": wire_owner(owner),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _mqtt_connection_kwargs(
    *,
    username: str | None,
    password: str | None,
    use_tls: bool,
    tls_context: ssl.SSLContext | None = None,
    ca_file: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> dict[str, Any]:
    """Build the auth/TLS kwargs shared by MQTTEventSink/MQTTEventSource's aiomqtt.Client.

    Without `use_tls` the connection is plaintext, which is only for a local development
    broker (see .devcontainer/stack/mqtt/mosquitto.conf); `validate_mqtt_security` has
    already refused credentials on it unless `allow_insecure`. With it, the context is the
    caller's own `tls_context`, or one built from `ca_file`/`client_cert`/`client_key`
    that verifies the broker and refuses TLS below 1.2 (see `build_mqtt_tls_context`).
    """
    kwargs: dict[str, Any] = {"username": username, "password": password}
    if use_tls:
        kwargs["tls_context"] = build_mqtt_tls_context(
            tls_context=tls_context, ca_file=ca_file, client_cert=client_cert, client_key=client_key
        )
    return kwargs


# Largest Session Expiry Interval MQTT 5.0 can carry (a four-byte unsigned integer).
_MAX_SESSION_TTL = 2**32 - 1

# Strong references for MQTTEventSource._events's detached disconnect tasks -- asyncio
# only holds a *weak* reference to a task once nothing else does, so a fire-and-forget
# `asyncio.ensure_future(...)` with no reference kept anywhere is eligible for garbage
# collection mid-run (see the stdlib docs' own "Save a reference to the result" note on
# asyncio.create_task); `add_done_callback` below is what lets each entry clean itself
# up once its disconnect actually finishes, so this doesn't grow unboundedly.
_background_tasks: set[asyncio.Task[None]] = set()


class _ReleasingEvents:
    """The `AsyncIterator` `EventSource.subscribe` returns: an async generator plus a release.

    `subscribe()` acquires its resources (an MQTT client, a queue registration) eagerly, so
    a bare async generator cannot own them: a generator that was never started has no
    `finally` to run, and `aclose()` on it is a no-op. This wrapper holds the one-shot
    `release` callable instead. `aclose()` runs it first, synchronously, then closes the
    generator; garbage collection runs it too (best-effort, see `__del__`). The generator
    itself calls the same `release` in its own `finally`, and the once-guard makes the
    repeats harmless.

    Callers should `aclose()` the iterator (for example with `contextlib.aclosing`), as
    for any async generator (PEP 525, PEP 533); only `aclose()` and garbage collection are
    guaranteed to release what the subscription holds.
    """

    def __init__(
        self,
        make_events: Callable[[Callable[[], None]], AsyncGenerator[dict[str, Any]]],
        release: Callable[[], None],
    ) -> None:
        released = False

        def release_once() -> None:
            nonlocal released
            if not released:
                released = True
                release()

        self._release = release_once
        self._events = make_events(release_once)

    def __aiter__(self) -> _ReleasingEvents:
        return self

    async def __anext__(self) -> dict[str, Any]:
        return await self._events.__anext__()

    async def aclose(self) -> None:
        """Release the subscription's resources, whether or not it was ever iterated."""
        # Release before awaiting anything: when the caller is itself being cancelled, a
        # further await here is cancelled again, but the release has already been started.
        self._release()
        try:
            await self._events.aclose()
        except RuntimeError:
            # A `__anext__()` is still running (a cancelled consumer task that has not
            # unwound yet); that task's cancellation runs the generator's `finally`.
            logger.debug("event iterator busy during aclose", exc_info=True)

    def __del__(self) -> None:
        """Best-effort release when the iterator is dropped; never raises."""
        try:
            self._release()
        except Exception:  # pragma: no cover -- defensive; __del__ must not raise
            logger.debug("event iterator release failed during GC", exc_info=True)


_PUBLISH_TIMEOUT_SECONDS = 10.0
"""Longest one connect-and-publish may take before it counts as a broker failure."""


class _SharedPublisher:
    """One broker connection shared by every `MQTTEventSink` with the same settings.

    The client is opened lazily on the first publish and kept, so a bulk write of a
    thousand rows does one CONNECT, not a thousand. Publishes are serialised under a lock
    (they stay in order). A publish that fails on a connection that had been idle is
    retried once on a fresh one (the broker may have dropped it); any other failure
    discards the client so the next publish reconnects, and is raised to the caller
    (`craftainer_crud.interfaces.delivery` logs and counts it). `aiomqtt.Client` binds
    to the running loop, so a publish from a different loop starts over.
    """

    def __init__(self, sink: MQTTEventSink) -> None:
        self._sink = sink
        self._client: aiomqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> aiomqtt.Client:
        import aiomqtt  # noqa: PLC0415 -- optional `[mqtt]` extra, only needed here

        sink = self._sink
        client = aiomqtt.Client(
            hostname=sink.hostname,
            port=sink.port,
            keepalive=sink.keepalive,
            **_mqtt_connection_kwargs(
                username=sink.username,
                password=sink.password,
                use_tls=sink.use_tls,
                tls_context=sink.tls_context,
                ca_file=sink.ca_file,
                client_cert=sink.client_cert,
                client_key=sink.client_key,
            ),
        )
        await client.__aenter__()
        self._client = client
        return client

    async def _discard(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                logger.debug("MQTT publisher disconnect failed", exc_info=True)

    async def publish(self, topic: str, payload: str) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # A client (and the lock) from another loop cannot be used or closed here.
            self._loop, self._client, self._lock = loop, None, asyncio.Lock()
        async with self._lock, asyncio.timeout(_PUBLISH_TIMEOUT_SECONDS):
            reused = self._client is not None
            try:
                try:
                    client = self._client or await self._connect()
                    await client.publish(topic, payload, qos=1)
                except Exception:
                    await self._discard()
                    if not reused:
                        raise
                    client = await self._connect()
                    await client.publish(topic, payload, qos=1)
            except BaseException:
                # A failure, timeout or cancellation leaves the client in an unknown
                # state: never reuse it.
                await asyncio.shield(self._discard())
                raise

    async def close(self) -> None:
        """Disconnect the shared client, if any."""
        async with self._lock:
            await self._discard()


_publishers: dict[tuple[object, ...], _SharedPublisher] = {}


async def close_event_publishers() -> None:
    """Disconnect every shared MQTT publisher connection (call on application shutdown).

    Waits for the after-commit deliveries already started first, so a committed change is
    not lost to the shutdown.
    """
    await drain_event_deliveries()
    publishers = list(_publishers.values())
    _publishers.clear()
    for publisher in publishers:
        await publisher.close()


@dataclass(frozen=True)
class MQTTEventSink:
    """EventSink publishing to a real MQTT broker at QoS 1 over one shared connection.

    Every sink with the same broker settings shares one `aiomqtt.Client`, opened on the
    first `publish()` and reused (see `_SharedPublisher`), so a bulk write is one CONNECT
    rather than one per event. The client is created lazily on the running loop, not at
    construction, because `aiomqtt.Client` binds to the event loop it is built on (which
    rules out building one at import time the way
    craftainer_crud.repositories.memory.InMemoryRepository is, see
    craftainer_crud.interfaces.dependency.build_event_sink_provider). Call
    `close_event_publishers()` on shutdown. QoS 1 here only guarantees this publish reaches
    the broker at least once; the "no missed events across a disconnect" guarantee
    (docs/adrs/0015-mqtt-for-crud-events.md) comes from the broker's own persistent-session
    queuing on the *subscribe* side (MQTTEventSource below), not from anything this class
    does.

    `use_tls` with `tls_context`, or `ca_file`/`client_cert`/`client_key`, selects the
    TLS context (see `MQTTConfig`); credentials without `use_tls` are refused unless
    `allow_insecure`. `password` is kept out of `repr`.
    """

    hostname: str
    port: int
    resource: str
    keepalive: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_tls: bool = False
    tls_context: ssl.SSLContext | None = None
    ca_file: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    allow_insecure: bool = False

    def __post_init__(self) -> None:
        """Refuse an insecure or self-contradicting connection setup."""
        _validate_sink_or_source_security(self)

    def _pool_key(self) -> tuple[object, ...]:
        return (
            self.hostname,
            self.port,
            self.keepalive,
            self.username,
            self.password,
            self.use_tls,
            self.tls_context,
            self.ca_file,
            self.client_cert,
            self.client_key,
        )

    async def publish(
        self,
        *,
        resource: str,
        record_id: int,
        action: str,
        snapshot: dict[str, Any],
        owner: object = None,
    ) -> None:
        """Publish one event to `crud-events/<resource>` at QoS 1."""
        envelope = _event_envelope(
            resource=resource,
            record_id=record_id,
            action=action,
            snapshot=snapshot,
            owner=owner,
        )
        key = self._pool_key()
        publisher = _publishers.get(key)
        if publisher is None:
            publisher = _publishers[key] = _SharedPublisher(self)
        await publisher.publish(f"crud-events/{resource}", json.dumps(envelope))


def _validate_sink_or_source_security(connection: MQTTEventSink | MQTTEventSource) -> None:
    """Apply `validate_mqtt_security` to a sink's or source's own fields."""
    validate_mqtt_security(
        username=connection.username,
        password=connection.password,
        use_tls=connection.use_tls,
        allow_insecure=connection.allow_insecure,
        tls_context=connection.tls_context,
        ca_file=connection.ca_file,
        client_cert=connection.client_cert,
        client_key=connection.client_key,
    )


@dataclass(frozen=True)
class MQTTEventSource:
    """EventSource subscribing to a real MQTT broker via a persistent session at QoS 1.

    `subscribe()` derives the MQTT client id from `subscriber_id` (issuing a new
    signed id if the caller has none, or none this server minted for this resource) and
    connects with `clean_session=False` --
    the broker then keeps a queue of QoS-1 messages published to `crud-events/
    <resource>` while this client id is disconnected (bounded by
    `.devcontainer/stack/mqtt/mosquitto.conf`'s `max_queued_messages`/
    `message_expiry_interval`, see docs/adrs/0015-mqtt-for-crud-events.md) and
    delivers them once the same client id reconnects. The connection is held open
    for as long as the returned iterator is consumed (typically the lifetime of one
    SSE stream, see craftainer_crud.controllers.crud_router's `GET <prefix>/events`) via the
    `async with` inside `_events`, not by this method itself.

    `subscribe(..., durable=False)` connects with `clean_session=True` instead (MQTT 3.1.1
    section 3.1.2.4): the broker discards the session when the connection ends, cleanly or
    not, so a restarting long-lived consumer leaves nothing behind. This stays on MQTT
    3.1.1 whatever `session_ttl` is.

    `session_ttl` (seconds, default 3600) bounds an *abandoned* durable session: a durable
    subscriber then connects over MQTT 5.0 with Clean Start 0 and a Session Expiry Interval
    of `session_ttl` (MQTT 5.0 sections 3.1.2.4 and 3.1.2.11.2), so the broker deletes the
    session, with its queued events, that long after the connection ends. A client that
    returns within the TTL resumes and is replayed exactly as before (QoS 1, same
    topic); one that returns later gets a fresh session. None keeps the previous behaviour
    (MQTT 3.1.1, expiry left to the broker). The publisher (`MQTTEventSink`) stays on
    3.1.1, which the broker bridges to v5 subscribers.
    """

    hostname: str
    port: int
    resource: str
    keepalive: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_tls: bool = False
    session_ttl: int | None = DEFAULT_SESSION_TTL_SECONDS
    tls_context: ssl.SSLContext | None = None
    ca_file: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    allow_insecure: bool = False

    def __post_init__(self) -> None:
        """Refuse an insecure setup, or a TTL the Session Expiry Interval cannot carry."""
        _validate_sink_or_source_security(self)
        ttl: object = self.session_ttl
        if ttl is not None and (
            not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= _MAX_SESSION_TTL
        ):
            msg = f"session_ttl must be an int between 1 and {_MAX_SESSION_TTL} seconds, or None"
            raise ValueError(msg)

    def _session_kwargs(self, *, durable: bool) -> dict[str, Any]:
        """The `aiomqtt.Client` kwargs that select the session type (see the class docstring)."""
        if not durable or self.session_ttl is None:
            return {"clean_session": not durable}
        import aiomqtt  # noqa: PLC0415 -- optional `[mqtt]` extra, only needed here
        from paho.mqtt.packettypes import PacketTypes as PahoPacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        # paho's Properties.__init__ is unannotated, which mypy --strict refuses to call.
        new_properties = cast("Callable[[int], Properties]", Properties)
        properties = new_properties(PahoPacketTypes.CONNECT)
        properties.SessionExpiryInterval = self.session_ttl
        # paho's default clean_start (FIRST_ONLY) would wipe the session on the very first
        # connect, so Clean Start 0 has to be explicit; v5 forbids `clean_session`.
        return {
            "protocol": aiomqtt.ProtocolVersion.V5,
            "clean_start": False,
            "properties": properties,
        }

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        """Resolve subscriber_id, connect and subscribe (a persistent session, or a clean
        one when `durable=False`), and return it with an async iterator of event
        envelopes from that point on.

        Pass `durable=False` with a fresh id (`None`), never one a durable subscriber is
        using: the clean-session CONNECT takes over that client id and discards its
        stored session and queued events.

        Connecting and subscribing *here*, before returning, rather than lazily on the
        iterator's first `__anext__()`, is what makes the delivery guarantee this class
        promises actually hold: craftainer_crud.controllers.crud_router's `_sse_events` sends
        its
        first (`id: 0`) frame -- the caller's signal that `subscriber_id` is now safely
        registered and safe to reconnect with -- as soon as `subscribe()` returns, and
        a caller is free to disconnect the instant it sees that frame (this is exactly
        what tests/integration/crud_1/heroes/test_heroes_v2_events.py does). Deferring
        the actual MQTT CONNECT/SUBSCRIBE to the iterator's first step raced that: a
        caller could disconnect -- cancelling the not-yet-connected iterator -- before
        this client ID's session was ever registered with the broker at all, so a
        message published in the gap had no persistent session to queue against and
        was simply dropped, never replayed on reconnect.

        The price of connecting eagerly is that the returned iterator owns a live client
        before anyone iterates it, so it is a `_ReleasingEvents`, not a bare generator:
        its `aclose()` disconnects the client even when it was never started, and
        garbage collection does so best-effort (a `ResourceWarning`, no disconnect, when
        no event loop is running). Callers must `aclose()` it, for example with
        `contextlib.aclosing`; see docs/adrs/0006-release-unstarted-event-iterators.md.
        """
        resolved = resolve_subscriber_id(self.resource, subscriber_id)
        import aiomqtt  # noqa: PLC0415 -- optional `[mqtt]` extra, only needed here

        session_kwargs = self._session_kwargs(durable=durable)
        client = aiomqtt.Client(
            hostname=self.hostname,
            port=self.port,
            identifier=f"crud-events-{self.resource}-{resolved}",
            keepalive=self.keepalive,
            **session_kwargs,
            **_mqtt_connection_kwargs(
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
                tls_context=self.tls_context,
                ca_file=self.ca_file,
                client_cert=self.client_cert,
                client_key=self.client_key,
            ),
        )
        await client.__aenter__()
        loop = asyncio.get_running_loop()
        try:
            await client.subscribe(f"crud-events/{self.resource}", qos=1)
        except BaseException:
            # No iterator exists yet to own the client, so release it here (detached, so
            # a cancellation of this call cannot cut the disconnect short).
            _schedule_disconnect(client, loop)
            raise
        return resolved, _ReleasingEvents(
            lambda release: self._events(client, release),
            lambda: _schedule_disconnect(client, loop),
        )

    async def _events(
        self, client: aiomqtt.Client, release: Callable[[], None]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield decoded event envelopes from `client`'s already-subscribed session,
        disconnecting once the caller stops consuming (return, exception, or
        cancellation -- see `subscribe()`'s own docstring for why connecting happens
        there rather than here).

        Disconnecting is a detached background task, not a plain `await` in this
        generator's own `finally` -- when the caller stops consuming because the ASGI
        server cancelled it (the common case: a client disconnected), this generator's
        own `finally` runs inside that same cancellation, and any further `await`
        there (confirmed via tests/integration/crud_1/heroes/test_heroes_v2_events.py,
        run with tracing) is immediately cancelled again rather than allowed to
        complete -- observed as aiomqtt's own disconnect-acknowledgement wait raising
        `CancelledError` before it could send a clean MQTT DISCONNECT. A `Task` started
        here, by contrast, is independent of this generator's own cancellation and gets
        to actually finish disconnecting.
        """
        try:
            async for message in client.messages:
                yield json.loads(message.payload)
        finally:
            release()


def _schedule_disconnect(client: aiomqtt.Client, loop: asyncio.AbstractEventLoop) -> None:
    """Start the detached disconnect task for `client` on `loop`, the loop that owns it.

    `loop` is the loop `subscribe()` ran on: the client's network loop and asyncio
    primitives are bound to it, so the disconnect must run there even when garbage
    collection triggers this from another thread or while another loop is running. If
    that loop is closed nothing can finish an async disconnect: warn instead of raising.

    Best-effort: this runs detached (see `MQTTEventSource._events`), so there is no
    caller left to usefully react to a disconnect failure -- catching it here (rather
    than letting it become an "exception was never retrieved" log) still logs it, so a
    broker-side auth/network anomaly during teardown leaves a trace instead of vanishing
    silently.
    """
    if loop.is_closed():
        warnings.warn(
            "MQTT event iterator released after its event loop closed; "
            "its client was not disconnected",
            ResourceWarning,
            stacklevel=2,
        )
        return

    async def _disconnect() -> None:
        try:
            await client.__aexit__(None, None, None)
        except Exception:  # pragma: no cover -- a real broker's clean disconnect never fails
            logger.debug("MQTT disconnect failed", exc_info=True)

    def _start() -> None:
        task = loop.create_task(_disconnect())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _start()
    else:
        loop.call_soon_threadsafe(_start)


class InMemoryEventSink:
    """EventSink *and* EventSource for MODE=mock, an asyncio.Queue fan-out per resource.

    One instance is built once and shared (see craftainer_crud.interfaces.dependency.
    build_event_sink_provider/build_event_source_provider), matching how
    craftainer_crud.repositories.memory.InMemoryRepository is built once and shared -- a
    single instance satisfies both EventSink (`publish`) and EventSource
    (`subscribe`) structurally, since under MODE=mock there's no broker to keep the
    two sides independent through: a `publish()` call needs to reach every
    currently-subscribed queue directly.

    This is necessarily best-effort, with **no delivery guarantee**: there's no
    broker, no persistent session, and no queued replay for a subscriber that's
    briefly disconnected -- a dropped SSE connection under MODE=mock simply misses
    whatever was published while it was down. The delivery guarantee in
    docs/adrs/0015-mqtt-for-crud-events.md only applies to the real
    MQTTEventSink/MQTTEventSource-backed path.
    """

    def __init__(self, resource: str | None = None) -> None:
        """Start with no subscribers.

        `resource` scopes the sink the way MQTTEventSource scopes by topic: when set,
        `publish()` drops events for any other resource. The default `None` broadcasts
        every published event to every subscriber, for callers that build their own
        sink. craftainer_crud.interfaces.dependency always builds one scoped sink per
        resource label, so routers sharing a label share one stream.
        """
        self._resource = resource
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}

    async def publish(
        self,
        *,
        resource: str,
        record_id: int,
        action: str,
        snapshot: dict[str, Any],
        owner: object = None,
    ) -> None:
        """Push one event onto every currently-subscribed queue, unless this sink is
        scoped to a different resource, in which case the event is dropped.
        """
        if self._resource is not None and resource != self._resource:
            return
        envelope = _event_envelope(
            resource=resource,
            record_id=record_id,
            action=action,
            snapshot=snapshot,
            owner=owner,
        )
        for queue in list(self._queues.values()):
            queue.put_nowait(envelope)

    async def subscribe(
        self, subscriber_id: str | None, *, durable: bool = True
    ) -> tuple[str, EventIterator[dict[str, Any]]]:
        """Register a queue under `subscriber_id`, or a newly minted id if it is None or forged.

        `durable` is accepted for EventSource compatibility and has no effect: the queue is
        dropped when the iterator ends either way.
        """
        del durable
        resolved = resolve_subscriber_id(self._resource or "", subscriber_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[resolved] = queue

        def release() -> None:
            # Only drop our own queue: the id may have been re-registered since.
            if self._queues.get(resolved) is queue:
                del self._queues[resolved]

        return resolved, _ReleasingEvents(lambda once: self._events(queue, once), release)

    async def _events(
        self, queue: asyncio.Queue[dict[str, Any]], release: Callable[[], None]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Yield events as they're published, forever -- caller stops iterating on disconnect.

        `finally` drops this subscriber's queue from `self._queues` once the caller
        stops consuming (return, exception, or cancellation) -- without it, every SSE
        connect/disconnect cycle (each issuing its own `uuid4` subscriber_id) leaves a
        queue behind for the rest of the process's lifetime, growing `self._queues`
        unboundedly under repeated reconnects.
        """
        try:
            while True:
                yield await queue.get()
        finally:
            release()


@dataclass(frozen=True)
class OwnerScope:
    """Opt-in per-user/per-tenant scoping for a CRUDInterface.

    Restricts `update`/`delete`/`update_many`/`delete_many` (always) and
    `get`/`list`/`count` (when `read_scoped` is True, the default) to records
    where `field == value` -- typically `value` is the caller's claims["sub"],
    resolved at CRUD-dependency-build time (see app.crud_1.heroes.get_hero_crud
    for the per-request build pattern this attaches to) -- and stamps `field`
    with `value` on create so a caller can't create a record owned by someone
    else.

    `read_scoped=False` opens reads to every caller while keeping writes
    owner-restricted: every authenticated caller sees every record via `get`/
    `list`/`count`, but can only `update`/`delete` (single or bulk) the records
    they themselves created -- see `app.crud_1.heroes.heroes_v2` for why Hero
    uses this shape rather than the fully-scoped default.

    Deliberately a CRUDInterface-level concept, not a Repository one: passing
    `owner=None` (the default) changes nothing, so a resource that never opts
    in is unaffected, and craftainer_crud.repositories stays unaware "ownership" exists at
    all -- see docs/adrs/0011-owner-scoped-crud-example-resource.md.
    """

    field: str
    value: Any
    read_scoped: bool = True

    def __post_init__(self) -> None:
        """Refuse a scope with no usable owner value instead of matching unowned rows.

        `value=None` would filter `field IS NULL` (every unowned/legacy row) and stamp
        NULL on create; `""` would group every caller without an identity. Both fail
        closed: building the scope raises, so the request errors instead of widening.
        """
        if self.value is None or self.value == "":
            raise ValueError(
                f"OwnerScope({self.field!r}) needs a non-empty owner value; got {self.value!r}"
                " (does the caller's token lack a subject claim?)"
            )

    def filter(self) -> FilterClause:
        """Return the equality FilterClause this scope adds to every query."""
        return FilterClause(self.field, FilterOp.EQ, self.value)


def event_visible(crud: object, event: Mapping[str, Any]) -> bool:
    """Return whether the caller behind `crud` may receive `event` (the `read_scoped` rule).

    Mirrors `CRUDInterface.get`'s visibility: a CRUD without an owner scope, or with
    `read_scoped=False`, sees every event; a read-scoped one sees only events whose
    envelope `owner` equals its own scope value. An event with no `owner` (a publisher
    that predates the field, or one injected on the topic) is dropped for a read-scoped
    caller -- fail closed. A `CompatCRUD` is looked through to the interface it wraps.
    """
    inner = getattr(crud, "_crud", crud)
    return scope_event_visible(getattr(inner, "_owner", None), event)


def scope_event_visible(owner: OwnerScope | None, event: Mapping[str, Any]) -> bool:
    """Return whether a caller holding `owner` (None: unscoped) may receive `event`.

    The rule `event_visible` applies, for a host whose `scope_dependency` yields just the
    scope (no CRUD, so no DB session held for the stream's life).
    """
    if owner is None or not owner.read_scoped:
        return True
    event_owner = event.get("owner")
    return event_owner is not None and event_owner == wire_owner(owner.value)


class CRUDLike[SchemaT: BaseModel](Protocol):
    """Structural shape both CRUDInterface and CompatCRUD satisfy.

    Lets craftainer_crud.controllers.crud_router's router factories depend on "anything with
    these methods" rather than concretely on CRUDInterface, so the same
    factory builds both a current-version router (backed by CRUDInterface) and a
    deprecated one (backed by craftainer_crud.interfaces.compat.CompatCRUD) identically.
    """

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> SchemaT | None:
        """Return the record with the given id as a view, or None if it doesn't exist."""
        ...

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> list[SchemaT]:
        """Return up to `limit` matching records as views, skipping the first `skip`."""
        ...

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters."""
        ...

    async def create(self, data: BaseModel) -> SchemaT:
        """Create a record from the given input view and return it as a view."""
        ...

    async def update(
        self, record_id: int, data: BaseModel, *, expected_version: datetime | None
    ) -> SchemaT | None:
        """Apply the given input view's set fields to the record, if it exists.

        `expected_version` is the record's `updated_at` the caller last saw (None: write
        unconditionally); a record at another version raises PreconditionFailedError.
        """
        ...

    async def delete(self, record_id: int, *, expected_version: datetime | None) -> bool:
        """Delete the record with the given id; return whether it existed.

        `expected_version` as in `update`.
        """
        ...

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: BaseModel
    ) -> Sequence[SchemaT]:
        """Apply the given input view's set fields to every matching record; return them."""
        ...

    async def delete_many(
        self, *, filters: Sequence[FilterClause], include_unpublished: bool = True
    ) -> Sequence[SchemaT]:
        """Delete every record matching the filters; return the records that were deleted.

        `include_unpublished=False` leaves not-yet-or-no-longer-published rows alone.
        """
        ...

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
        """Return aggregate statistics for records matching the given filters."""
        ...


class CRUDInterface[SchemaT: BaseModel, ModelT]:
    """CRUD operations for one resource, parameterized by its view and repository."""

    def __init__(
        self,
        schema: type[SchemaT],
        repository: Repository[ModelT],
        *,
        owner: OwnerScope | None = None,
        revisions: RevisionSink | None = None,
        events: EventSink | None = None,
        resource: str | None = None,
        actor: str = "unknown",
    ) -> None:
        """Bind this interface to the view/repository it converts through.

        `owner`, if given, restricts every operation below to records this owner
        created -- see OwnerScope's own docstring.

        `revisions`, if given, is called once per successful create/update/
        update_many/delete/delete_many with a snapshot of the affected record --
        `revisions=None` (the default) changes nothing, the same opt-in shape as
        `owner`. `resource` (e.g. "hero") and `actor` (typically the caller's
        `claims["sub"]`, resolved once per request the same way `owner`'s `value`
        is) are only meaningful when `revisions` is set.

        `events`, if given, is called once per successful create/update/update_many/
        delete/delete_many **and** restore/restore_many -- broader than `revisions`
        above, see EventSink's own docstring for why. `events=None` (the default)
        changes nothing, the same opt-in shape as `owner`/`revisions`. `resource` is
        shared with `revisions` above; `events` doesn't use `actor`.
        """
        self._schema = schema
        self._repository = repository
        self._owner = owner
        self._revisions = revisions
        self._events = events
        self._resource = resource
        self._actor = actor

    async def _record_revision(self, *, record_id: int, action: str, snapshot: SchemaT) -> None:
        """Call the configured RevisionSink, if any, with a JSON-safe snapshot."""
        if self._revisions is None:
            return  # pragma: no cover -- see module docstring
        await self._revisions.record(
            resource=self._resource or "",
            record_id=record_id,
            action=action,
            snapshot=snapshot.model_dump(mode="json"),
            actor=self._actor,
        )

    async def _publish_event(self, *, record_id: int, action: str, snapshot: SchemaT) -> None:
        """Hand the configured EventSink, if any, a JSON-safe snapshot to publish after commit.

        Delivery is after-commit and best effort (see craftainer_crud.interfaces.delivery):
        nothing is published for a rolled-back transaction, and a sink failure is logged
        and counted, not raised.

        Called alongside `_record_revision` for create/update/update_many/delete/
        delete_many, and additionally for restore/restore_many -- see EventSink's
        own docstring for why this is broader than `_record_revision`'s scope.
        """
        if self._events is None:
            return  # pragma: no cover -- see module docstring
        # `owner` is passed only for a scoped resource, so a custom EventSink written
        # before the parameter existed keeps working for an owner-less one.
        owner_kwargs = {} if self._owner is None else {"owner": self._owner.value}
        await deliver_event(
            self._events,
            session=getattr(self._repository, "session", None),
            resource=self._resource or "",
            record_id=record_id,
            action=action,
            snapshot=snapshot.model_dump(mode="json"),
            **owner_kwargs,
        )

    def _update_values(self, data: BaseModel) -> dict[str, Any]:
        """Return the update's set fields, minus the owner field.

        Ownership is stamped on create only; an update that names the owner field
        (a host view that exposes it) must not hand the record to another owner, so
        the field is dropped here rather than trusted from the view.
        """
        values = data.model_dump(exclude_unset=True)
        if self._owner is not None:
            values.pop(self._owner.field, None)
        return values

    def _scoped(self, filters: Sequence[FilterClause]) -> Sequence[FilterClause]:
        """Add this interface's owner filter (if any) to a write operation's filters.

        Always applied when `owner` is set, regardless of `owner.read_scoped` --
        writes stay owner-restricted even when reads are opened up to everyone.
        """
        return filters if self._owner is None else (*filters, self._owner.filter())

    def _read_scoped(self, filters: Sequence[FilterClause]) -> Sequence[FilterClause]:
        """Add this interface's owner filter (if any) to a read operation's filters.

        Unlike `_scoped`, this is a no-op when `owner.read_scoped` is False --
        `get`/`list`/`count` then see every record, not just this owner's.
        """
        if self._owner is None or not self._owner.read_scoped:
            return filters
        return (*filters, self._owner.filter())  # pragma: no cover -- see module docstring

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> SchemaT | None:
        """Return the record with the given id as a view, or None if it doesn't exist.

        `include_archived`/`include_unpublished` override the default Archivable/
        Schedulable exclusion for a model carrying those mixins -- a no-op
        otherwise, see
        craftainer_crud.repositories.sqlalchemy/craftainer_crud.repositories.memory.
        """
        if self._owner is None or not self._owner.read_scoped:
            instance = await self._repository.get(
                record_id,
                include_archived=include_archived,
                include_unpublished=include_unpublished,
            )
            return self._schema.model_validate(instance) if instance is not None else None
        matches = await self._repository.list(  # pragma: no cover -- see module docstring
            filters=self._read_scoped(_id_filter(record_id)),
            limit=1,
            include_archived=include_archived,
            include_unpublished=include_unpublished,
        )
        return (  # pragma: no cover -- see module docstring
            self._schema.model_validate(matches[0]) if matches else None
        )

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> list[SchemaT]:
        """Return up to `limit` matching records as views, skipping the first `skip`.

        See `get`'s docstring for `include_archived`/`include_unpublished`.
        """
        instances = await self._repository.list(
            skip=skip,
            limit=limit,
            filters=self._read_scoped(filters),
            sort=sort,
            include_archived=include_archived,
            only_archived=only_archived,
            include_unpublished=include_unpublished,
        )
        return [self._schema.model_validate(instance) for instance in instances]

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        only_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters.

        Called by craftainer_crud.controllers.crud_actions before a bulk update/delete, to cap
        how many records a single action can affect.
        """
        return await self._repository.count(
            filters=self._read_scoped(filters),
            include_archived=include_archived,
            only_archived=only_archived,
            include_unpublished=include_unpublished,
        )

    async def create(self, data: BaseModel) -> SchemaT:
        """Create a record from the given input view and return it as a view.

        When `owner` is set, the owned field is stamped from the scope rather than
        trusted from `data` -- a caller can't create a record owned by someone else.

        `exclude_unset=True`: a field the caller genuinely omitted (as opposed to one
        explicitly set to its own default) is left out of `values` entirely, so the
        repository/column's own default applies -- required for a Draftable create
        (see craftainer_crud.controllers.crud_router's `/draft` route, whose body validates
        against a resource's all-optional `*Update` view): an omitted non-nullable
        lifecycle field like Lockable's `is_locked` must take its column default
        (e.g. `False`), not an explicit `None`, which every normal create-schema
        field (always required, so always "set") is unaffected by.
        """
        values = data.model_dump(exclude_unset=True)
        if self._owner is not None:  # pragma: no cover -- see module docstring
            values[self._owner.field] = self._owner.value
        instance = await self._repository.create(values)
        result = self._schema.model_validate(instance)
        await self._record_revision(
            record_id=instance.id,  # type: ignore[attr-defined]
            action="create",
            snapshot=result,
        )
        await self._publish_event(
            record_id=instance.id,  # type: ignore[attr-defined]
            action="create",
            snapshot=result,
        )
        return result

    async def update(
        self, record_id: int, data: BaseModel, *, expected_version: datetime | None
    ) -> SchemaT | None:
        """Apply the given input view's set fields to the record, if it exists.

        Always the repository's single-record `update`, restricted to the owner scope when
        there is one (a record outside it is not found); `expected_version` makes the
        write conditional -- see craftainer_crud.repositories.base.Repository.update.
        """
        instance = await self._repository.update(
            record_id,
            self._update_values(data),
            expected_version=expected_version,
            scope=self._scoped(()),
        )
        if instance is None:
            return None
        result = self._schema.model_validate(instance)
        await self._record_revision(record_id=record_id, action="update", snapshot=result)
        await self._publish_event(record_id=record_id, action="update", snapshot=result)
        return result

    async def delete(self, record_id: int, *, expected_version: datetime | None) -> bool:
        """Delete the record with the given id; return whether it existed.

        Owner scope and `expected_version` behave as in `update`.
        """
        snapshot = (
            await self._pre_delete_snapshot(record_id)
            if self._revisions is not None or self._events is not None
            else None
        )
        deleted = await self._repository.delete(
            record_id, expected_version=expected_version, scope=self._scoped(())
        )
        if deleted and snapshot is not None:
            await self._record_revision(record_id=record_id, action="delete", snapshot=snapshot)
            await self._publish_event(record_id=record_id, action="delete", snapshot=snapshot)
        return deleted

    async def _pre_delete_snapshot(self, record_id: int) -> SchemaT | None:
        """Return a view of the record before it's deleted, for the revision log/event stream."""
        instance = await self._repository.get(
            record_id, include_archived=True, include_unpublished=True
        )
        return self._schema.model_validate(instance) if instance is not None else None

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: BaseModel
    ) -> Sequence[SchemaT]:
        """Apply the given input view's set fields to every matching record; return them."""
        instances = await self._repository.update_many(
            filters=self._scoped(filters), data=self._update_values(data)
        )
        results = [self._schema.model_validate(instance) for instance in instances]
        for result in results:
            await self._record_revision(record_id=result.id, action="update", snapshot=result)  # type: ignore[attr-defined]
            await self._publish_event(record_id=result.id, action="update", snapshot=result)  # type: ignore[attr-defined]
        return results

    async def delete_many(
        self, *, filters: Sequence[FilterClause], include_unpublished: bool = True
    ) -> Sequence[SchemaT]:
        """Delete every record matching the filters; return the records that were deleted.

        `include_unpublished` defaults to True (unlike `list`): a bulk delete reaches
        scheduled rows unless the caller passes False.
        """
        instances = await self._repository.delete_many(
            filters=self._scoped(filters), include_unpublished=include_unpublished
        )
        results = [self._schema.model_validate(instance) for instance in instances]
        for result in results:
            await self._record_revision(record_id=result.id, action="delete", snapshot=result)  # type: ignore[attr-defined]
            await self._publish_event(record_id=result.id, action="delete", snapshot=result)  # type: ignore[attr-defined]
        return results

    async def restore(self, record_id: int) -> SchemaT | None:
        """Clear `archived_at` on the record with the given id; return it, or None.

        Unlike delete/update, restore has no `revisions` counterpart (see
        RevisionSink's own docstring) but does fire `events` -- a subscriber
        watching visibility changes cares about a record becoming visible again,
        see EventSink's own docstring.
        """
        if self._owner is None:  # pragma: no cover -- see module docstring
            instance = await self._repository.restore(record_id)
            result = self._schema.model_validate(instance) if instance is not None else None
        else:
            restored = await self._repository.restore_many(
                filters=self._scoped(_id_filter(record_id))
            )
            result = self._schema.model_validate(restored[0]) if restored else None
        if result is not None:
            await self._publish_event(record_id=record_id, action="restore", snapshot=result)
        return result

    async def restore_many(self, *, filters: Sequence[FilterClause]) -> Sequence[SchemaT]:
        """Clear `archived_at` on every record matching the filters; return them."""
        instances = await self._repository.restore_many(filters=self._scoped(filters))
        results = [self._schema.model_validate(instance) for instance in instances]
        for result in results:
            await self._publish_event(record_id=result.id, action="restore", snapshot=result)  # type: ignore[attr-defined]
        return results

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
        """Return aggregate statistics for records matching the given filters.

        A thin pass-through to the repository, applying the same owner-scoped
        read restriction (`_read_scoped`) `get`/`list`/`count` already apply --
        `stats` is a generic, read-only operation like `count`, not
        resource-specific, so it belongs here rather than in a resource's own
        controller (see ../interfaces/README.md's "Do"/"Don't").
        """
        return await self._repository.stats(
            numeric_fields=numeric_fields,
            categorical_fields=categorical_fields,
            filters=self._read_scoped(filters),
            bucket=bucket,
            include_archived=include_archived,
            include_unpublished=include_unpublished,
        )


def _id_filter(record_id: int) -> tuple[FilterClause]:
    """Return a single-element filter sequence matching one record by id."""
    return (FilterClause("id", FilterOp.EQ, record_id),)
