"""Per-caller cap on concurrent `GET <prefix>/events` streams.

Each stream holds an MQTT connection (and, for a durable one, a broker session) plus an
asyncio task, so an unbounded number per caller is a resource-exhaustion lever. The cap
(`CrudConfig.sse_max_streams_per_caller`, default 5) is counted in this process only: with
several workers the effective cap is the limit times the worker count.

The caller key is the owner scope's value when the resource has an `OwnerScope`, shared
across every resource (one user, one budget). A caller without an owner scope cannot be
told apart from any other, so all of them share one bucket per resource: safe by default
(the cap still bounds the resource), at the price that those callers compete for it. Give
such a resource an `OwnerScope` (even with `read_scoped=False`) for a per-caller budget, or
raise the cap.
"""

from collections.abc import Hashable
from dataclasses import dataclass, field

from craftainer_crud.interfaces.base import OwnerScope


def caller_key(owner: OwnerScope | None, resource: str) -> Hashable:
    """Return the key a caller's streams are counted under (see the module docstring)."""
    if owner is None:
        return ("unscoped", resource)
    return ("owner", str(owner.value))


@dataclass(slots=True)
class StreamLease:
    """One counted stream; `release()` returns it to the caller's budget, once."""

    _limiter: StreamLimiter
    _key: Hashable
    _released: bool = field(default=False, init=False)

    def release(self) -> None:
        """Give the slot back; calling it again does nothing."""
        if not self._released:
            self._released = True
            self._limiter._release(self._key)


class StreamLimiter:
    """Counts open streams per caller key and refuses past a limit."""

    def __init__(self) -> None:
        """Start with no open streams."""
        self._counts: dict[Hashable, int] = {}

    def acquire(self, key: Hashable, limit: int | None) -> StreamLease | None:
        """Take a slot for `key`, or return None when it already holds `limit` (None: no cap)."""
        held = self._counts.get(key, 0)
        if limit is not None and held >= limit:
            return None
        self._counts[key] = held + 1
        return StreamLease(self, key)

    def _release(self, key: Hashable) -> None:
        remaining = self._counts[key] - 1
        if remaining:
            self._counts[key] = remaining
        else:
            del self._counts[key]

    def open_streams(self, key: Hashable) -> int:
        """Return how many streams `key` holds open (for tests and diagnostics)."""
        return self._counts.get(key, 0)


stream_limiter = StreamLimiter()
"""The process-wide limiter the generated `/events` routes use."""
