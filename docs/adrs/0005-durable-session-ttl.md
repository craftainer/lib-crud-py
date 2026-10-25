# 0005. Session TTL for durable event subscribers

## Status

Accepted. Completes the follow-up that [0004](0004-transient-event-subscribers.md)
left open.

## Context

Browser SSE clients that drop their subscriber id leave a durable (persistent) MQTT
session behind. Under MQTT 3.1.1 only the broker's own non-standard expiry bounds it
(Mosquitto `persistent_client_expiration`, off by default). MQTT 5.0 lets the client
set the bound itself: Session Expiry Interval in CONNECT (section 3.1.2.11.2) together
with Clean Start 0 (section 3.1.2.4).

## Decision

`MQTTEventSource(..., session_ttl: int | None = None)`, in seconds, 1 to 2**32-1
(`MQTTConfig.session_ttl_seconds` feeds it; the router and HTTP surface do not expose it).

- `session_ttl=None` (default): unchanged. MQTT 3.1.1, `clean_session=False`.
- `session_ttl=N`, `durable=True`: MQTT 5.0, Clean Start 0, Session Expiry Interval N,
  same topic and QoS 1. Clean Start must be set explicitly because paho's default
  (clean on the first connect) would wipe the session. A client that comes back within N
  seconds resumes and is replayed; later it gets a fresh session. A clean
  disconnect sends DISCONNECT without a Session Expiry Interval, so the CONNECT value
  stands (section 3.14.2.2.2: an absent value leaves the interval unchanged).
- `durable=False`: stays on 3.1.1 with `clean_session=True`, whatever `session_ttl` is.
- `MQTTEventSink` stays on 3.1.1. The broker delivers its QoS 1 publishes to v5 and v3
  subscribers alike (verified against Mosquitto 2.0.22 in
  `tests/integration/test_mqtt_sessions.py`).

On Mosquitto 2.0.22 with `persistent_client_expiration 1h` set, a client-set interval of
2s took effect (session gone after about 5s), so the client value wins over that global.
This result is empirical: neither the Mosquitto man page nor the changelog documents the
interaction, so re-check it on a broker upgrade. Operators can cap client-set intervals
broker-side with `max_session_expiry_interval`.

## Consequences

- Additive: nothing changes unless `session_ttl` is set. Custom `EventSource`s are unaffected.
- Needs a broker with MQTT 5.0 support and a client id that is new or already used by a
  session that was created over v5 or v3 (the broker resumes either).
- The broker-side backstop in the README stays advisable for brokers or clients that
  ignore the interval.
- Removing `session_ttl` later, or rolling back, makes the same id resume over 3.1.1.
  What the broker does with the expiry of a session created over v5 is broker-specific
  and was not verified; treat such sessions as unbounded until they are drained, and keep
  the broker-side backstop.
- The v5 path imports `paho.mqtt` directly, so `paho-mqtt>=2.1.0,<3` (the range aiomqtt
  2.5.1 requires) is declared in the `mqtt` extra rather than left to aiomqtt's own
  dependencies.
