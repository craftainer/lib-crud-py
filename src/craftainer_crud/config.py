"""Runtime injection point for the host application's engine and settings.

`craftainer_crud` never reads application settings or builds a database
engine itself. The host calls `configure(...)` once at startup, before the first
request, passing an `async_sessionmaker` (it owns the engine) and a `CrudConfig`.
Until then `get_config()` returns the defaults below and `get_session_factory()`
raises, so a missing wiring step fails loudly instead of silently using the wrong
database.
"""

import math
import ssl
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MIN_SUBSCRIBER_SECRET_LEN = 32
MAX_MQTT_KEEPALIVE_SECONDS = 65535
"""MQTT 3.1.1 section 3.1.2.10: the keep-alive is a 16-bit unsigned count of seconds."""


DEFAULT_SESSION_TTL_SECONDS = 3600
"""Default Session Expiry Interval of a durable event-stream session (one hour)."""
DEFAULT_MAX_STREAMS_PER_CALLER = 5
"""Default cap on concurrent `/events` streams per caller."""


def validate_mqtt_security(  # noqa: PLR0913 -- one keyword per MQTTConfig field it checks
    *,
    username: str | None,
    password: str | None,
    use_tls: bool,
    allow_insecure: bool,
    tls_context: ssl.SSLContext | None,
    ca_file: str | None,
    client_cert: str | None,
    client_key: str | None,
) -> None:
    """Refuse an MQTT connection setup that is insecure or contradicts itself.

    Shared by `MQTTConfig`, `MQTTEventSink` and `MQTTEventSource`, so a hand-built sink
    gets the same checks as one built from the config. Raises `ValueError`:

    - credentials (`username`/`password`) without `use_tls`, unless `allow_insecure`:
      they would cross the network in clear text on CONNECT;
    - any TLS material (`tls_context`, `ca_file`, `client_cert`, `client_key`) without
      `use_tls`: the setting would be silently ignored;
    - `tls_context` together with a file option (pick one way to describe the trust);
    - `client_key` without `client_cert`;
    - a `tls_context` that allows TLS below 1.2 or does not verify the broker
      (`CERT_NONE`), unless `allow_insecure`.
    """
    material = tls_context is not None or any((ca_file, client_cert, client_key))
    if not use_tls:
        if (username is not None or password is not None) and not allow_insecure:
            msg = (
                "MQTT credentials without use_tls=True would be sent in clear text; "
                "enable TLS or pass allow_insecure=True (local development only)"
            )
            raise ValueError(msg)
        if material:
            msg = "tls_context, ca_file, client_cert and client_key need use_tls=True"
            raise ValueError(msg)
        return
    if tls_context is not None and any((ca_file, client_cert, client_key)):
        msg = "pass either tls_context or ca_file/client_cert/client_key, not both"
        raise ValueError(msg)
    if client_key is not None and client_cert is None:
        msg = "client_key needs client_cert"
        raise ValueError(msg)
    if tls_context is not None and not allow_insecure:
        if tls_context.minimum_version < ssl.TLSVersion.TLSv1_2:
            msg = "tls_context must set minimum_version to TLS 1.2 or newer"
            raise ValueError(msg)
        if tls_context.verify_mode == ssl.CERT_NONE:
            msg = "tls_context must verify the broker certificate (not CERT_NONE)"
            raise ValueError(msg)
        if not tls_context.check_hostname:
            msg = "tls_context must verify the broker host name (check_hostname=True)"
            raise ValueError(msg)


def build_mqtt_tls_context(
    *,
    tls_context: ssl.SSLContext | None,
    ca_file: str | None,
    client_cert: str | None,
    client_key: str | None,
) -> ssl.SSLContext:
    """Return the `ssl.SSLContext` an MQTT connection uses: the given one, or one built here.

    A built context verifies the broker certificate and host name against `ca_file` (or
    the system trust store when None), refuses TLS below 1.2, and presents
    `client_cert`/`client_key` when given (mutual TLS). The files are read on each
    call, so a rotated certificate is picked up by the next connection.
    """
    if tls_context is not None:
        return tls_context
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_cert is not None:
        context.load_cert_chain(client_cert, keyfile=client_key)
    return context


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    """Connection details for the MQTT event sink/source (needs the future `[mqtt]` extra).

    Secrets are kept out of `repr` (the password), so logging the config leaks nothing.
    """

    host: str = "localhost"
    port: int = 1883
    keepalive_seconds: int = 60
    """MQTT keep-alive in seconds, 0 to 65535 (MQTT 3.1.1 section 3.1.2.10; 0 disables it)."""
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_tls: bool = False
    tls_context: ssl.SSLContext | None = None
    """A ready `ssl.SSLContext` for the broker connection (needs `use_tls=True`). Must allow
    TLS 1.2 or newer only and verify the broker. Mutually exclusive with the three file
    options below."""
    ca_file: str | None = None
    """PEM bundle that signs the broker's certificate (a private CA). None: the system
    trust store. Needs `use_tls=True`."""
    client_cert: str | None = None
    """PEM client certificate (chain) for mutual TLS. Needs `use_tls=True`."""
    client_key: str | None = None
    """PEM private key of `client_cert`; None when the key is in the certificate file."""
    allow_insecure: bool = False
    """Allow credentials over a connection without TLS (local development brokers only),
    and a `tls_context` that does not verify the broker or allows TLS below 1.2."""
    session_ttl_seconds: int | None = DEFAULT_SESSION_TTL_SECONDS
    """Seconds the broker keeps an abandoned durable event-stream session (MQTT 5.0 Session
    Expiry Interval); default 3600. None leaves expiry to the broker. See
    `MQTTEventSource.session_ttl`."""

    def __post_init__(self) -> None:
        """Refuse an insecure setup, or a keep-alive outside the 16-bit range MQTT allows."""
        validate_mqtt_security(
            username=self.username,
            password=self.password,
            use_tls=self.use_tls,
            allow_insecure=self.allow_insecure,
            tls_context=self.tls_context,
            ca_file=self.ca_file,
            client_cert=self.client_cert,
            client_key=self.client_key,
        )
        keepalive: object = self.keepalive_seconds
        if not isinstance(keepalive, int) or isinstance(keepalive, bool):
            msg = "keepalive_seconds must be an int"
            raise TypeError(msg)
        if not 0 <= keepalive <= MAX_MQTT_KEEPALIVE_SECONDS:
            msg = f"keepalive_seconds must be between 0 and {MAX_MQTT_KEEPALIVE_SECONDS}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CrudConfig:
    """Everything `craftainer_crud` needs from its host, besides the session factory."""

    use_memory: bool = False
    """True selects the in-memory repository/event bus instead of SQLAlchemy/MQTT (mock mode)."""
    bulk_action_max_matched: int = 1000
    """Largest number of records one bulk update/delete/restore may match."""
    sse_keepalive_seconds: float = 15.0
    """Idle seconds before an SSE stream emits a keep-alive comment. Must be finite and
    greater than 0: 0 would spin the stream and is advertised to clients in the manifest."""
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    sse_max_streams_per_caller: int | None = DEFAULT_MAX_STREAMS_PER_CALLER
    """Most concurrent `GET <prefix>/events` streams one caller may hold open (per process);
    the next one is refused with 429. The caller is the owner scope's value, or, without an
    owner scope, one shared bucket per resource. None removes the cap."""
    sse_durable_sessions: bool = False
    """False (the default): every `/events` stream uses a transient subscription (clean
    session, no replay), and a `subscriber_id`/`Last-Event-ID` the client sends is ignored.
    True: streams are durable (the broker queues events while the client is away and replays
    them to a returning id), bounded by `MQTTConfig.session_ttl_seconds`."""
    subscriber_secret: bytes | None = field(default=None, repr=False)
    """Key that signs event-stream subscriber ids, at least 32 bytes. None (the default)
    uses a random per-process secret, so ids do not survive a restart. Set it (the same
    value on every instance) to let a client resume across restarts and workers. Keep it
    like any signing key: whoever holds it can mint ids."""

    def __post_init__(self) -> None:
        """Refuse a bad SSE keep-alive or stream cap, or a bad subscriber secret."""
        sse: object = self.sse_keepalive_seconds
        if not isinstance(sse, int | float) or isinstance(sse, bool):
            msg = "sse_keepalive_seconds must be a number"
            raise TypeError(msg)
        try:
            as_float = float(sse)
        except OverflowError:  # an int too large for a float is not finite
            as_float = math.inf
        if not math.isfinite(as_float) or as_float <= 0:
            msg = "sse_keepalive_seconds must be finite and greater than 0"
            raise ValueError(msg)
        cap: object = self.sse_max_streams_per_caller
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            msg = "sse_max_streams_per_caller must be an int of at least 1, or None"
            raise ValueError(msg)
        secret: object = self.subscriber_secret
        if secret is None:
            return
        if not isinstance(secret, bytes):
            msg = "subscriber_secret must be bytes"
            raise TypeError(msg)
        if len(secret) < MIN_SUBSCRIBER_SECRET_LEN:
            msg = f"subscriber_secret must be at least {MIN_SUBSCRIBER_SECRET_LEN} bytes"
            raise ValueError(msg)


_config = CrudConfig()
_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure(
    config: CrudConfig, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> None:
    """Install the host's config and session factory (call once at startup)."""
    global _config, _session_factory  # noqa: PLW0603 -- the module is the injection point
    _config = config
    _session_factory = session_factory


def get_config() -> CrudConfig:
    """Return the installed config (defaults until `configure` is called)."""
    return _config


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the installed session factory, or raise if `configure` hasn't supplied one.

    The raise is `# pragma: no cover` for tests/e2e specifically: its one live process
    is always configured, so the branch can never run there --
    tests/unit/craftainer_crud/test_config.py covers it directly.
    """
    if _session_factory is None:  # pragma: no cover -- see docstring
        msg = "craftainer_crud.config.configure(...) must be called with a session factory"
        raise RuntimeError(msg)
    return _session_factory
