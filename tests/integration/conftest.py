"""Integration fixtures: skip without POSTGRES_* env vars, own the `heroes` table for the run."""

from collections.abc import AsyncIterator
from typing import cast

import pytest
from sqlalchemy import Table

from tests.integration.db import engine, missing_vars
from tests.support.hero_model import Hero


@pytest.fixture(autouse=True, scope="session")
async def _heroes_table() -> AsyncIterator[None]:
    """Create the `heroes` table before the suite and drop it afterwards if this run made it.

    Point POSTGRES_DB at a dedicated database: a `heroes` table that already exists
    is left alone, but the suite writes rows into it.
    """
    missing = missing_vars()
    if missing:
        pytest.skip(f"integration tests need a Postgres: {', '.join(missing)} not set")
    table = cast(Table, Hero.__table__)
    async with engine.begin() as conn:
        existed = await conn.run_sync(lambda sync: sync.dialect.has_table(sync, table.name))
        await conn.run_sync(table.create, checkfirst=True)
    yield
    if not existed:
        async with engine.begin() as conn:
            await conn.run_sync(table.drop)
