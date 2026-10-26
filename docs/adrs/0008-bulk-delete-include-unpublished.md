# 0008. Bulk delete honours `include_unpublished`

## Status

Accepted.

## Context

A Schedulable record outside its `publish_at`/`unpublish_at` window is hidden from list
and get unless `?include_unpublished=true`. Bulk `PATCH`/`DELETE`/restore never applied
that exclusion (the repositories hard-code `include_unpublished=True` for them), because
an editor must be able to correct or remove a scheduled record. A view that hides
unpublished records then offers "delete all matching" and would also delete the records
it is hiding. The reserved query keys are part of the cross-implementation contract in
[`docs/manifest.md`](../manifest.md), so the answer is mirrored by other servers and the
SDKs.

## Decision

We will let a bulk `DELETE` (no `?id=`) read the reserved key `include_unpublished`. Unlike
list, whose default is `false`, the default here is `true`: an absent flag keeps today's
behaviour, and `include_unpublished=false` leaves not-yet-or-no-longer-published records
alone. It uses the same boolean vocabulary and `422` on a bad value as list. Archived rows
are excluded either way, as before, and drafts (`is_draft`) are not "unpublished" (filter
them with `is_draft=false`). The response `{matched, ids}` and the pre-delete size cap
count exactly the rows the flag selects. `Repository.delete_many`, `CRUDLike.delete_many`
and `CRUDInterface.delete_many` gain `include_unpublished: bool = True`. Single-record
delete by id is unchanged, and so are bulk update and restore.

## Consequences

Callers that never send the flag see no change. A custom `Repository` or `CRUDLike`
implementation must accept the new keyword. The size cap now counts scheduled rows
when the flag is `true` (before, it counted only published rows and could under-count).
`update_many` and `restore_many` still ignore the publication window; a flag for them
would be a separate decision.
