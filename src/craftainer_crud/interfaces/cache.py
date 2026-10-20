"""Opt-in shared cache for the generic `/stats` and `/predict` routes.

A small Protocol, the same opt-in shape as `RevisionSink`/`EventSink` in
`base.py`: `craftainer_crud` never depends on Redis (or any cache
library). The host supplies an implementation (see `app.cache.RedisStatsCache`)
and passes it to the router factories as `stats_cache=`; `None` (the default)
changes nothing.

Only non-per-user aggregates are cached. The router builds each opaque `key`
from the route kind, its normalized query parameters, the response schema
and the caller's ownership scope (so an owner-scoped resource never shares an
entry between owners). The implementation owns everything else: the TTL, any
namespacing (e.g. the app version, so a response whose shape changed between
releases is never served across them), and how `invalidate` finds a
resource's entries.
"""

from typing import Protocol


class StatsCache(Protocol):
    """Shared cache of serialized `/stats` and `/predict` response bodies, per resource."""

    async def get(self, resource: str, key: str) -> bytes | None:
        """Return the bytes stored under `resource`/`key`, or None on a miss or expired entry."""
        ...

    async def set(self, resource: str, key: str, value: bytes) -> None:
        """Store `value` under `resource`/`key`, overwriting any previous entry."""
        ...

    async def invalidate(self, resource: str) -> None:
        """Drop every entry stored for `resource` (called after any of its writes)."""
        ...
