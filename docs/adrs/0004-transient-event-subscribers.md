# 0004. Transient event subscribers

## Status

Accepted

## Context

Since 0.3, subscriber ids are server-minted and signed (ADR 0003). A long-lived
in-process subscriber, such as an application's cache-invalidation task, gets a new id
on every restart, so its previous persistent MQTT session (`clean_session=False`) is
never resumed and stays on the broker with whatever it queued. Options considered:
broker-side session expiry only, a stable id derived from `SUBSCRIBER_SECRET`, explicit
release on shutdown (which does not cover a crash), transient subscribers, and an
MQTT v5 session expiry interval.

## Decision

`EventSource.subscribe(subscriber_id, *, durable=True)`. With `durable=False`,
`MQTTEventSource` connects with a clean session (MQTT 3.1.1 section 3.1.2.4), so the
broker discards the session when the connection ends, cleanly or by crash. The default
is unchanged, and the generated router never exposes `durable`, because SSE clients rely
on replay. The READMEs recommend a finite persistent-session expiry on production
brokers (Mosquitto never expires by default; EMQX defaults to 2h). A v5 session expiry
interval for durable sessions follows separately.

## Consequences

- Restarting a transient consumer orphans nothing; it also gets no replay.
- Custom `EventSource` implementations must accept the `durable` keyword.
- Abandoned durable SSE sessions are bounded only by the broker's expiry setting until
  the v5 follow-up lands.
