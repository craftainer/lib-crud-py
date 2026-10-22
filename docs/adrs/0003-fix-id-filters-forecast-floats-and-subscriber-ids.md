# 0003. Reject id-with-filters, forecast on time, finite floats, signed subscriber ids

## Status

Accepted

## Context

A review of the Rust port (work item AX1b) found four places where this server was
wrong, not merely different: a single-record mutation silently ignored filters
(`DELETE ?id=5&status=draft` deleted record 5 whatever its status), the forecast
regressed on position in the series so a gap skewed the trend, `nan`/`inf` were
accepted as float filter values, and any client-chosen `subscriber_id` resumed a
live session. The Rust server fixes the same four so both stay identical.

## Decision

- `?id=` with any non-reserved query key on get, update and delete is a 422.
- The forecast regresses on bucket time (whole buckets since the first bucket);
  the exact formula is in `docs/manifest.md`.
- `nan` and `inf` in a float filter are a 422.
- Subscriber ids are `nonce(128 bit) + HMAC-SHA256 tag(128 bit)` over the resource
  and nonce, keyed by `CrudConfig.subscriber_secret` (at least 32 bytes, random per
  process by default). A resume id that does not verify is replaced by a new one.
  A MAC was chosen over a registry so no per-subscriber state is kept and ids
  survive restarts when the host supplies the secret.

## Consequences

- Clients that sent filters with `?id=` now get 422. The SDKs send one or the other.
- Forecast values change for series with empty buckets (and, for months, none
  otherwise: equal-width calendar months keep a linear series linear).
- Ids are 64 hex characters instead of a UUID, and a client-invented id no longer
  resumes anything. Clients treat the id as opaque, so nothing client-side changes.
- Without `subscriber_secret`, ids do not survive a restart or span workers.
