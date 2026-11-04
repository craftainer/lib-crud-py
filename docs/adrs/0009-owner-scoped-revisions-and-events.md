# 0009. Owner-scoped revisions and events

## Status

Accepted.

## Context

An OWASP review found two broken-access-control holes for a resource built with
`OwnerScope(read_scoped=True)`. `GET <prefix>/revisions?id=` queried the shared
`Revision` table directly, so any caller with `read_roles` read another owner's
history (full snapshots and the actor's `sub`) by guessing an id. `GET <prefix>/events`
fanned every event of the resource to every subscriber, snapshot included. Four smaller
gaps sat beside them: the owner field could be changed by an update, `OwnerScope(value=None)`
filtered `IS NULL`, `skip`/`limit` had no lower bound (a negative `limit` in mock mode
bypassed the 1000-row cap), and `icontains` passed `%`/`_` through as LIKE wildcards.

## Decision

`/revisions` resolves the record through `crud.get(id, include_archived=True,
include_unpublished=True)` first and answers the same 404 as `GET ?id=` when it is not
visible. The event envelope gains an additive `owner` member (the scope's value, `null`
without a scope; non-JSON values as `str()`) for broker-side routing and filtering only:
`/events` strips it from the frame it sends, so a subscriber never learns another user's
value from the envelope. `EventSink.publish` gains an optional
`owner` keyword, and `/events` drops, per subscriber, every event a read-scoped caller
could not `get`, including events with no `owner`. The topic layout is unchanged.
`OwnerScope` raises `ValueError` for `None` or `""`, `update`/`update_many` drop the owner
field from the data, list routes bound `skip` to 0..1,000,000 and `limit` to 1..1000, and the SQL
`icontains` escapes `\`, `%` and `_`.

## Consequences

`build_json_router`, `build_xml_router` and `build_router` take an optional
`scope_dependency` that returns only the `OwnerScope` (or `None`), for example built from the
token. `/events` then resolves it instead of `crud_dependency`, so no DB session is held for
the stream's life; the filtering is the same (`scope_event_visible`). Without it `/events`
falls back to `crud_dependency`, which stays open, with any session it yields, until the
stream ends.

A minor release: the envelope and `EventSink.publish` gain a member, and `CRUDInterface`
passes `owner` only for a scoped resource, so existing sinks keep working for owner-less
resources. A scoped resource with a custom sink must accept `owner`. `/revisions` for a
hard-deleted record is now a 404 (it was a 200 with the history); archived records still
resolve. Resources without a scope or with `read_scoped=False` see every event as before,
and the envelope's `owner` stays on the broker (the SSE frame omits it), so they learn nothing
beyond the record snapshot. A token without a subject claim
now makes building the scope fail (a 500 from the host's dependency) instead of widening
access. Events stay published before commit, and unbounded streams remain; separate items.
