# 0007. Add an `only_archived` list query parameter

## Status

Accepted.

## Context

An Archivable model hides archived rows from a list. The only way to see them was
`?include_archived=true`, which returns live and archived rows together, so a client that
wants just the archived ones (a trash view) had to page through every row and discard the
live ones. `archived_at` is not a filter field (filters are built from the view schema, and
archived state is lifecycle metadata, not part of it), so `archived_at__min=` and friends
are not available either. The reserved query keys are part of the cross-implementation
contract in [`docs/manifest.md`](../manifest.md), so the answer has to be mirrored by
other servers and the SDKs.

## Decision

We will add a reserved boolean query key `only_archived` (default `false`) to list routes.
`true` returns only the rows with `archived_at` set and combines with filters, `sort`,
`skip` and `limit` like `include_archived`. It implies `include_archived`; `only_archived=true`
next to an explicit `include_archived=false` is a `422`. Non-Archivable models ignore it, as
they ignore `include_archived`. The repository `list`/`count` methods take a matching
`only_archived` keyword. We rejected making `archived_at` a general filter field: it would
widen the filter grammar (operators, value types, manifest field descriptions) for one use.

## Consequences

A trash view is one filtered, paged query. `Repository` and `CRUDLike` implementations
gain a keyword argument (defaulted, so callers are unaffected, but a custom implementation
must accept it). The flag is a reserved key, so a model field named `only_archived` can no
longer be filtered on, the same as the other reserved keys. The key is read from the raw
query string and is not declared in OpenAPI, like `include_archived`. `/stats`, the write routes
and a single-record `?id=` read answer `422` when it is `true`, so a caller cannot act on
live rows by mistake.
