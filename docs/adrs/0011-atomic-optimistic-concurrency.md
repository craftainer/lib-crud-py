# 0011. Atomic optimistic concurrency via `expected_version`

## Status

Accepted. Breaking change to the `Repository`, `CRUDInterface` and `CRUDLike` contracts
(a `0.x` minor release; see the README's "Versioning").

## Context

`If-Match` was enforced by the router: `crud.get`, compare the record's `ETag`, then call
`update`/`delete`. The comparison and the write were separate statements, so two writers
that both passed the comparison both wrote (lost update). The controllers README documented
this as a known race. A review (OW-lib-crud-py) flagged it, and the user chose to close it in
the repository contract rather than document it again.

Two facts of the code shaped the fix. There is no stored version column: the only version
the record has is `updated_at` (server `now()` on insert and on update), and the `ETag` is
a digest of `id` and `updated_at` computed from the *view*, which cannot be turned back into
a column value. And an owner-scoped resource (every `OwnerScope`) never reached
`Repository.update`/`delete`: `CRUDInterface` routed its single-record writes through
`update_many`/`delete_many` with an owner filter, which in `SQLAlchemyRepository` are an ORM
select followed by a flush (`UPDATE ... WHERE id`), so they could not carry a version
predicate either.

## Decision

- **Version = `updated_at`.** No schema change, no change to the `ETag` or the wire format.
- `Repository.update` and `Repository.delete` (and `CRUDInterface`/`CRUDLike`/`CompatCRUD`)
  take a required keyword `expected_version: datetime | None`. `None` is the explicit
  opt-out for an unconditional write; a value makes the write happen only if the stored
  `updated_at` equals it. Required, with no default, so a caller can't opt out by forgetting.
- They also take `scope: Sequence[FilterClause] = ()`, extra predicates the record must
  satisfy; a record outside it is not found (`None`/`False`, never a precondition failure).
  `CRUDInterface` always calls the single-record methods with `scope=self._scoped(())`, i.e.
  the owner filter, so `OwnerScope` and `expected_version` compose. `scope` is plain
  `FilterClause`s, so the repository still knows nothing about "ownership" (ADR 0011 of the
  example host application keeps its layering). `update_many`/`delete_many` are unchanged.
- `SQLAlchemyRepository` runs one conditional Core statement per write -- `UPDATE ... WHERE
  id = :id AND <scope> AND updated_at = :expected [AND archived_at IS NULL] RETURNING <row>`,
  a soft-delete `UPDATE ... SET archived_at`, or `DELETE ... WHERE` -- and treats an empty
  result as the outcome. A fresh read first handles the early outcomes (not found, version
  mismatch, locked) with `populate_existing`, so it never trusts a stale identity-map copy.
  When a conditional write matches no row, a read under the same predicates (minus the
  version) separates "removed meanwhile" (`None`/`False`) from "changed meanwhile"
  (`PreconditionFailedError`). The explicit `updated_at = now()` in the `SET` list means even
  an empty PATCH moves the version.
- `InMemoryRepository` checks and writes in one step with no `await` between, so on the event
  loop it is atomic; its `updated_at` is forced strictly after the previous value, so a fast
  second write can't leave the version unchanged.
- A mismatch raises the new `repositories.base.PreconditionFailedError`.
  `controllers.crud_actions` maps it to the same `412` and detail text a stale `If-Match`
  already gets. The router derives `expected_version` from `If-Match` (still strong
  comparison): after matching the `ETag` against `crud.get`, it passes the matched record's
  `updated_at`. `If-Match: *`, no `If-Match`, a bulk action, a missing record and a view with
  no `updated_at` pass `None`. HTTP status codes and semantics are unchanged: no `428`
  for a missing `If-Match`.

## Consequences

- Of two writers holding the same version exactly one wins; the other gets `412`. The
  router's `crud.get` comparison is kept for the early `412` and to obtain the version.
- `updated_at` is only as good as its clock. Postgres `now()` is the transaction's start time,
  so two writes in one transaction share a version, and the column has microsecond
  granularity (OW L-2): two commits in the same microsecond could share a value. The in-memory
  backend guards this itself; a host whose database can't tell two commits apart needs a real
  version column.
- A view with no `updated_at` still gets the pre-write `ETag` comparison but no atomic check.
- The `is_locked` check is still a read before the write (a race with a concurrent lock is not
  closed here).
- **Migration for callers** (e.g. template-fastapi): every call to `Repository.update`/`delete`,
  `CRUDInterface.update`/`delete` and `CompatCRUD.update`/`delete` must add
  `expected_version=` (`None` keeps today's behaviour). A custom `Repository` or `CRUDLike`
  implementation must accept `expected_version` and `scope`, perform the check atomically with
  the write, and raise `PreconditionFailedError`. A host that calls `resolve_update`/
  `resolve_delete` directly must pass `expected_version`. Code that catches `RecordLockedError`
  around these calls should handle `PreconditionFailedError` too (the shipped routers do).
