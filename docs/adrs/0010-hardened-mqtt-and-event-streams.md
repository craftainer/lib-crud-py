# 0010. Hardened MQTT connection, after-commit events and bounded event streams

## Status

Accepted. Amends [0004](0004-transient-event-subscribers.md) (the generated router now
subscribes transient unless the host opts into durable sessions) and
[0005](0005-durable-session-ttl.md) (the session TTL now defaults to one hour).

## Context

An OWASP review of the library found four gaps in the event path beside the owner-scope
holes [0009](0009-owner-scoped-revisions-and-events.md) closed:

- The MQTT connection offered a bare `use_tls` flag: no private CA, no client certificate,
  no TLS floor, and `username`/`password` were accepted over plaintext (default port 1883).
  The password showed up in `repr`.
- Events were published inside the CRUD call, before the request's transaction committed,
  each over its own new broker connection. A bulk write of up to 1000 rows made up to 1000
  sequential connects while the transaction was open, a broker outage turned every write
  into a 500, and a rolled-back change had already been announced.
- Nothing bounded the `/events` streams per caller, and every stream was durable, so each
  GET without an id created a new persistent broker session that lived until the broker
  expired it (never, on Mosquitto's default).

## Decision

**TLS and credentials.** `MQTTConfig` (and `MQTTEventSink`/`MQTTEventSource`, which run the
same check) take `tls_context` (a ready `ssl.SSLContext`) or `ca_file`/`client_cert`/
`client_key` (a built context: verifies the broker and host name against the CA or the
system store, TLS 1.2 minimum, mutual TLS when a certificate is given; the files are read per
connection, so rotation needs no restart). `username`/`password` without `use_tls` raise
`ValueError` unless `allow_insecure=True`; TLS options without `use_tls`, a context together
with file options, and a key without a certificate raise too. A supplied context that allows
TLS below 1.2 or does not verify the broker is refused unless `allow_insecure`. `password`
and `CrudConfig.subscriber_secret` are `repr=False`. The default port stays 1883: the dataclass
cannot tell a defaulted port from an explicit one, so the host sets 8883 with TLS.

**Event delivery.** `CRUDInterface` hands events to `interfaces/delivery.py`. For a repository
bound to a SQLAlchemy session (`SQLAlchemyRepository.session`) the events are held on the
session and published, in order, in one background task after its `after_commit`; a real
rollback drops them. A repository without a session (in-memory) publishes immediately. A
sink failure is logged and counted (`event_delivery_stats()`), never raised, and not retried:
delivery is best effort. `MQTTEventSink` publishes over one shared client per distinct broker
setting (opened lazily on the running loop, one retry on a stale connection, a 10 s bound per
publish); `close_event_publishers()` closes them on shutdown. A savepoint rollback inside the
transaction does not retract earlier events; the library opens none.

**Event streams.** Each caller holds at most `CrudConfig.sse_max_streams_per_caller` (default
5, `None` for no cap) concurrent `/events` streams per process; the next is a 429. The caller
is the owner scope's value (from `scope_dependency`, else the CRUD's own scope), counted across
resources. Callers without an owner scope cannot be told apart, so all of them share one
bucket per resource: safe by default, at the price that they compete for it. Streams are
transient (clean session, no replay) unless `CrudConfig.sse_durable_sessions=True`; a transient
stream ignores a client's `subscriber_id`/`Last-Event-ID`, because a clean-session connect under
a durable id would discard that session. `MQTTConfig.session_ttl_seconds` and
`MQTTEventSource.session_ttl` default to 3600 (`None` still leaves expiry to the broker).
The manifest advertises `replay: persistent` only when durable sessions are on.

## Consequences

A minor release, with behaviour changes a host must know: credentials over plaintext now fail
at configuration time (set `use_tls`, or `allow_insecure` for a local broker); `/events` no
longer resumes unless the host sets `sse_durable_sessions=True`; durable sessions and a default
`session_ttl` use MQTT 5.0, so the broker must speak it (set `session_ttl_seconds=None` to stay on
3.1.1); events can be lost on a broker failure or a crash between commit and publish (a client
that needs completeness refetches on reconnect); and a host that builds its own sink must accept
being called after the request, from a background task. The stream cap is per process, so with
N workers the effective cap is N times the limit. Unscoped resources get a cap of five streams in
total until they pass an owner scope or raise the limit. Optimistic concurrency and the
SQLAlchemy 2.1 typing change remain separate items.
