# 0006. Release event subscriptions that are never iterated

## Status

Accepted. Refines the eager-registration rule recorded in
[`MQTTEventSource.subscribe`](../../src/craftainer_crud/interfaces/base.py) and in
template-fastapi's ADR 0015 (delivery guarantee).

## Context

`EventSource.subscribe()` acquires its resources before returning: `MQTTEventSource`
connects an `aiomqtt` client (and its network loop) and subscribes; `InMemoryEventSink`
registers a queue. This is deliberate. `GET <prefix>/events` sends its first frame, the
caller's signal that the durable session exists and is safe to reconnect with, as soon as
`subscribe()` returns, and a caller may disconnect at once. Connecting on the iterator's
first step would let that disconnect land before the broker ever saw the session, so events
published in the gap would be dropped instead of queued.

The returned iterator was a plain async generator whose cleanup lived in its `finally`.
A generator that was never started has no `finally` to run, and `aclose()` on it does
nothing. A caller cancelled between `subscribe()` returning and the first `__anext__`, or
one that dropped the iterator, left the client connected with its loop running (reproduced
against Mosquitto: the client was still connected after `aclose()` or garbage collection).

## Decision

Keep the connect and subscribe eager, and make the returned iterator own the release.

- `subscribe()` returns a small iterator (`__aiter__`, `__anext__`, `aclose`) that wraps
  the generator and a once-only `release` callable.
- The declared return type narrows from `AsyncIterator[dict]` to the new public
  `EventIterator[T]` (`craftainer_crud.interfaces.base`), a `typing.Protocol` that extends
  `AsyncIterator[T]` with `async def aclose(self) -> None`, so `contextlib.aclosing` type-checks
  without casts. This was a user decision.
- `aclose()` runs `release` first, synchronously, then closes the generator. For MQTT,
  `release` starts the same detached disconnect task as before, which survives the
  caller's own cancellation. For the in-memory source it drops the queue.
- Garbage collection runs `release` as well, best-effort. With no running event loop the
  MQTT disconnect cannot be started; it emits a `ResourceWarning` and never raises.
- `_sse_events` closes the iterator in its `finally`, and its first-frame `yield` is inside
  the `try`, so a cancel before the first event releases the subscription.
- Rejected: moving the connect into the generator body. It makes connect errors surface
  at the first step rather than at `subscribe()`, and it reintroduces the lost-event race
  above.
- Not added: an `async with` form or new `subscribe` parameters.

## Consequences

- External callers must `aclose()` the iterator, for example with
  `contextlib.aclosing`, as for any async generator (PEP 525, PEP 533). The library
  guarantees only that `aclose()` and garbage collection release the subscription.
- A task cancelled between `subscribe()` and the caller taking ownership of the iterator
  still leaks unless the caller closes it in a `finally`; GC is the backstop.
- Backward compatibility: `EventIterator` is a structural protocol, so an `EventSource`
  outside the package that returns a plain async generator (the usual implementation)
  satisfies it unchanged, because generators have `aclose()`. A source that returns an
  `AsyncIterator` without `aclose` fails type-checking against the narrower annotation
  (and `_sse_events` now calls `aclose()` unconditionally); it must return an iterator that
  has one.
