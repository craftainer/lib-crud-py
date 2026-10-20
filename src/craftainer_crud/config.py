"""Runtime injection point for the host application's engine and settings.

`craftainer_crud` never reads application settings or builds a database
engine itself. The host calls `configure(...)` once at startup, before the first
request, passing an `async_sessionmaker` (it owns the engine) and a `CrudConfig`.
Until then `get_config()` returns the defaults below and `get_session_factory()`
raises, so a missing wiring step fails loudly instead of silently using the wrong
database.
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class MQTTConfig:
    """Connection details for the MQTT event sink/source (needs the future `[mqtt]` extra)."""

    host: str = "localhost"
    port: int = 1883
    keepalive_seconds: int = 60
    username: str | None = None
    password: str | None = None
    use_tls: bool = False


@dataclass(frozen=True, slots=True)
class CrudConfig:
    """Everything `craftainer_crud` needs from its host, besides the session factory."""

    use_memory: bool = False
    """True selects the in-memory repository/event bus instead of SQLAlchemy/MQTT (mock mode)."""
    bulk_action_max_matched: int = 1000
    """Largest number of records one bulk update/delete/restore may match."""
    sse_keepalive_seconds: float = 15.0
    """Idle seconds before an SSE stream emits a keep-alive comment."""
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)


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
