"""Unit test: craftainer_crud.config's injection point and the session dependency."""

import ssl
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from craftainer_crud import config as crud_config
from craftainer_crud.config import (
    CrudConfig,
    MQTTConfig,
    build_mqtt_tls_context,
    configure,
    get_config,
    get_session_factory,
)
from craftainer_crud.controllers.crud_router import _rate_limited
from craftainer_crud.models.base import get_db


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo any configure(...) call made by a test."""
    monkeypatch.setattr(crud_config, "_config", crud_config._config)
    monkeypatch.setattr(crud_config, "_session_factory", crud_config._session_factory)


def test_configure_installs_config_and_session_factory() -> None:
    """configure(...) makes both retrievable."""
    factory = cast(async_sessionmaker[AsyncSession], object())
    config = CrudConfig(bulk_action_max_matched=5)

    configure(config, factory)

    assert get_config() is config
    assert get_session_factory() is factory


def test_get_session_factory_raises_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a session factory, asking for one fails loudly."""
    monkeypatch.setattr(crud_config, "_session_factory", None)
    with pytest.raises(RuntimeError, match="configure"):
        get_session_factory()


async def test_get_db_commits_a_session_from_the_injected_factory() -> None:
    """get_db yields a session from the injected factory and commits afterwards."""
    events: list[str] = []

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            events.append("closed")

        async def commit(self) -> None:
            events.append("commit")

    configure(CrudConfig(), cast(Any, _Session))

    gen = get_db()
    session: object = await anext(gen)
    assert isinstance(session, _Session)
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert events == ["commit", "closed"]


def test_rate_limited_applies_the_given_decorator_or_none() -> None:
    """_rate_limited returns the host's decorator, or an identity decorator by default."""

    def route() -> None: ...

    assert _rate_limited(None)(route) is route

    def decorator(func: Any) -> Any:  # noqa: ANN401 -- test double
        return "decorated"

    result: object = _rate_limited(decorator)(route)
    assert result == "decorated"


@pytest.mark.parametrize(
    "value", [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), 10**400]
)
def test_sse_keepalive_seconds_rejects_non_positive_or_non_finite(value: float) -> None:
    """0 would spin the keep-alive loop and 0 in the manifest makes clients reconnect forever."""
    with pytest.raises(ValueError, match="sse_keepalive_seconds"):
        CrudConfig(sse_keepalive_seconds=value)


@pytest.mark.parametrize("value", [0.01, 15.0, 3600])
def test_sse_keepalive_seconds_accepts_finite_positive(value: float) -> None:
    """Any finite positive value is kept as given."""
    assert CrudConfig(sse_keepalive_seconds=value).sse_keepalive_seconds == value


@pytest.mark.parametrize("value", [-1, 65536])
def test_mqtt_keepalive_seconds_rejects_out_of_range(value: int) -> None:
    """MQTT 3.1.1 section 3.1.2.10: a 16-bit unsigned value, 0 to 65535."""
    with pytest.raises(ValueError, match="keepalive_seconds"):
        MQTTConfig(keepalive_seconds=value)


@pytest.mark.parametrize("value", [0, 1, 60, 65535])
def test_mqtt_keepalive_seconds_accepts_spec_range(value: int) -> None:
    """0 (keep-alive disabled) through 65535 are valid."""
    assert MQTTConfig(keepalive_seconds=value).keepalive_seconds == value


@pytest.mark.parametrize("value", [None, "15", True])
def test_sse_keepalive_seconds_rejects_non_numbers(value: object) -> None:
    """A non-number (or a bool) is a TypeError, not a confusing comparison failure."""
    with pytest.raises(TypeError, match="sse_keepalive_seconds"):
        CrudConfig(sse_keepalive_seconds=cast(Any, value))


@pytest.mark.parametrize("value", [60.5, "60", None, True])
def test_mqtt_keepalive_seconds_rejects_non_ints(value: object) -> None:
    """The MQTT keep-alive is a 16-bit integer; anything else is a TypeError."""
    with pytest.raises(TypeError, match="keepalive_seconds"):
        MQTTConfig(keepalive_seconds=cast(Any, value))


def test_crud_config_carries_a_valid_mqtt_config() -> None:
    """A CrudConfig embeds an already-validated MQTTConfig."""
    assert CrudConfig(mqtt=MQTTConfig(keepalive_seconds=0)).mqtt.keepalive_seconds == 0


# --- MQTT TLS and credentials ------------------------------------------------

_FAKE_PASSWORD = "not-a-real-secret"  # noqa: S105 -- a stand-in


def _context(
    *, minimum: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2, verify: bool = True
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = minimum
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def test_credentials_without_tls_are_refused_unless_allow_insecure() -> None:
    """A username or password over a plaintext connection needs the explicit opt-out."""
    with pytest.raises(ValueError, match="clear text"):
        MQTTConfig(username="u")
    with pytest.raises(ValueError, match="clear text"):
        MQTTConfig(password=_FAKE_PASSWORD)
    assert MQTTConfig(username="u", password=_FAKE_PASSWORD, allow_insecure=True).use_tls is False
    assert MQTTConfig(username="u", password=_FAKE_PASSWORD, use_tls=True).use_tls is True
    assert MQTTConfig().username is None  # no credentials, no TLS: local development


def test_tls_material_without_use_tls_is_refused() -> None:
    """A TLS option that would be silently ignored is an error."""
    for options in ({"ca_file": "ca.pem"}, {"tls_context": _context()}):
        with pytest.raises(ValueError, match="need use_tls"):
            MQTTConfig(**options)


def test_contradicting_tls_options_are_refused() -> None:
    """A context excludes the file options, and a key needs its certificate."""
    with pytest.raises(ValueError, match="not both"):
        MQTTConfig(use_tls=True, tls_context=_context(), ca_file="ca.pem")
    with pytest.raises(ValueError, match="client_key needs client_cert"):
        MQTTConfig(use_tls=True, client_key="key.pem")
    assert MQTTConfig(use_tls=True, client_cert="cert.pem", client_key="key.pem").use_tls


def test_a_supplied_context_must_verify_and_be_tls_1_2_or_newer() -> None:
    """A weak context is refused unless allow_insecure; a sound one is accepted."""
    assert MQTTConfig(use_tls=True, tls_context=_context()).use_tls
    with pytest.raises(ValueError, match=r"TLS 1\.2"):
        MQTTConfig(use_tls=True, tls_context=_context(minimum=ssl.TLSVersion.MINIMUM_SUPPORTED))
    with pytest.raises(ValueError, match="verify the broker"):
        MQTTConfig(use_tls=True, tls_context=_context(verify=False))
    no_hostname = _context()
    no_hostname.check_hostname = False
    with pytest.raises(ValueError, match="host name"):
        MQTTConfig(use_tls=True, tls_context=no_hostname)
    weak = _context(verify=False)
    assert MQTTConfig(use_tls=True, tls_context=weak, allow_insecure=True).tls_context is weak


def test_secrets_are_not_in_repr() -> None:
    """The MQTT password and the subscriber secret stay out of repr (logs, tracebacks)."""
    config = CrudConfig(
        mqtt=MQTTConfig(username="u", password=_FAKE_PASSWORD, use_tls=True),
        subscriber_secret=b"s" * 32,
    )
    assert _FAKE_PASSWORD not in repr(config)
    assert "ssssssss" not in repr(config)


def test_a_built_context_verifies_and_refuses_old_tls() -> None:
    """Without a context or files the default trust store is used, floor TLS 1.2."""
    context = build_mqtt_tls_context(
        tls_context=None, ca_file=None, client_cert=None, client_key=None
    )
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_a_built_context_loads_the_ca_and_client_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """ca_file is the trust anchor and client_cert/client_key are presented (mutual TLS)."""
    seen: dict[str, Any] = {}
    context = _context()

    def create_default_context(*, cafile: str | None) -> ssl.SSLContext:
        seen["cafile"] = cafile
        return context

    def load_cert_chain(certfile: str, keyfile: str | None = None) -> None:
        seen["chain"] = (certfile, keyfile)

    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    monkeypatch.setattr(context, "load_cert_chain", load_cert_chain, raising=False)
    built = build_mqtt_tls_context(
        tls_context=None, ca_file="ca.pem", client_cert="c.pem", client_key="k.pem"
    )
    assert built is context
    assert seen == {"cafile": "ca.pem", "chain": ("c.pem", "k.pem")}


def test_a_supplied_context_is_used_as_is() -> None:
    """build_mqtt_tls_context hands a caller's own context back unchanged."""
    context = _context()
    assert (
        build_mqtt_tls_context(tls_context=context, ca_file=None, client_cert=None, client_key=None)
        is context
    )


def test_event_stream_defaults_and_validation() -> None:
    """Five streams per caller, transient sessions and a one-hour session TTL by default."""
    config = CrudConfig()
    assert config.sse_max_streams_per_caller == 5
    assert config.sse_durable_sessions is False
    assert config.mqtt.session_ttl_seconds == 3600
    assert CrudConfig(sse_max_streams_per_caller=None).sse_max_streams_per_caller is None
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="sse_max_streams_per_caller"):
            CrudConfig(sse_max_streams_per_caller=cast("int", bad))
