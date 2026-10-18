"""Async engine and session factory for the integration suite, built from POSTGRES_* env vars."""

import os

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_VARS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def missing_vars() -> list[str]:
    """Return the POSTGRES_* variables that are unset or empty."""
    return [name for name in _VARS if not os.environ.get(name)]


def _url() -> str:
    # With a variable missing the URL is a placeholder: building an engine never
    # connects, and conftest skips every test before one could use it.
    if missing_vars():
        return "postgresql+asyncpg://unset:unset@unset/unset"
    env = os.environ
    return (
        f"postgresql+asyncpg://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}"
        f"@{env['POSTGRES_HOST']}:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    )


# NullPool: an asyncpg connection is tied to the loop it was opened on, so a
# fresh connection per checkout keeps tests independent of loop lifetimes.
engine: AsyncEngine = create_async_engine(_url(), poolclass=NullPool)
session_factory = async_sessionmaker(engine, expire_on_commit=False)
