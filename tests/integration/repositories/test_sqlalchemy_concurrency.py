"""Integration: update()/delete() optimistic concurrency against a real Postgres.

Each race commits its rows (two sessions must see each other's writes), so every test
removes what it created in a `finally`, even on failure.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import Integer, Table
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import Mapped, mapped_column

from craftainer_crud.models.base import IdentifiedBase
from craftainer_crud.repositories.base import PreconditionFailedError, RecordLockedError
from craftainer_crud.repositories.filtering import FilterClause, FilterOp
from craftainer_crud.repositories.sqlalchemy import SQLAlchemyRepository
from tests.integration.db import engine
from tests.integration.db import session_factory as async_session_factory
from tests.support.hero_model import Hero


class _HardRecord(IdentifiedBase):
    """A model with no `archived_at`, so delete() issues a real DELETE."""

    __tablename__ = "concurrency_test_hard_records_integration"

    value: Mapped[int] = mapped_column(Integer, default=0)


_hard_table = cast(Table, _HardRecord.__table__)


@asynccontextmanager
async def _committed_hero(name: str = "Racer", owner: str = "alice") -> AsyncIterator[Hero]:
    """Commit one hero, yield it, then hard-delete the row whatever happened."""
    async with async_session_factory() as session:
        hero = await SQLAlchemyRepository(session, Hero).create({"name": name, "owner_id": owner})
        await session.commit()
    try:
        yield hero
    finally:
        async with async_session_factory() as session:
            await session.execute(sa_delete(Hero).where(Hero.id == hero.id))
            await session.commit()


async def _stored(hero_id: int) -> Hero | None:
    async with async_session_factory() as session:
        return await SQLAlchemyRepository(session, Hero).get(hero_id, include_archived=True)


async def test_two_updates_with_the_same_expected_version_exactly_one_wins() -> None:
    """Two writers holding one version: one UPDATE matches the row, the other is refused."""
    async with _committed_hero() as hero:
        version = hero.updated_at

        async def attempt(name: str) -> str | None:
            async with async_session_factory() as session:
                try:
                    await SQLAlchemyRepository(session, Hero).update(
                        hero.id, {"name": name}, expected_version=version
                    )
                except PreconditionFailedError:
                    await session.rollback()
                    return None
                await session.commit()
                return name

        outcomes = await asyncio.gather(attempt("first"), attempt("second"))
        winners = [name for name in outcomes if name is not None]
        assert len(winners) == 1
        stored = await _stored(hero.id)
        assert stored is not None
        assert stored.name == winners[0]
        assert stored.updated_at > version


async def test_two_deletes_with_the_same_expected_version_exactly_one_wins() -> None:
    """Same race for a (soft) delete: one archives the row, the other is refused."""
    async with _committed_hero() as hero:
        version = hero.updated_at

        async def attempt() -> bool:
            async with async_session_factory() as session:
                try:
                    deleted = await SQLAlchemyRepository(session, Hero).delete(
                        hero.id, expected_version=version
                    )
                except PreconditionFailedError:
                    await session.rollback()
                    return False
                await session.commit()
                return deleted

        outcomes = await asyncio.gather(attempt(), attempt())
        assert sorted(outcomes) == [False, True]
        stored = await _stored(hero.id)
        assert stored is not None
        assert stored.archived_at is not None


async def test_update_and_delete_with_a_stale_version_raise_and_change_nothing() -> None:
    """A version that isn't the stored one is refused for both writes."""
    async with _committed_hero() as hero:
        stale = hero.updated_at - timedelta(seconds=1)
        async with async_session_factory() as session:
            repository = SQLAlchemyRepository(session, Hero)
            with pytest.raises(PreconditionFailedError):
                await repository.update(hero.id, {"name": "X"}, expected_version=stale)
            with pytest.raises(PreconditionFailedError):
                await repository.delete(hero.id, expected_version=stale)
        stored = await _stored(hero.id)
        assert stored is not None
        assert (stored.name, stored.archived_at) == ("Racer", None)


async def test_update_with_the_current_version_writes_and_moves_the_version() -> None:
    """A matching version writes and returns the refreshed row; the old version is then stale."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        updated = await repository.update(hero.id, {"name": "Y"}, expected_version=hero.updated_at)
        assert updated is not None
        assert updated.name == "Y"
        assert updated.updated_at > hero.updated_at
        await session.commit()
        with pytest.raises(PreconditionFailedError):
            await repository.update(hero.id, {"name": "Z"}, expected_version=hero.updated_at)


async def test_none_opts_out_of_the_version_check() -> None:
    """expected_version=None writes and soft-deletes regardless of the stored version."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        assert await repository.update(hero.id, {"name": "A"}, expected_version=None)
        assert await repository.update(hero.id, {}, expected_version=None)
        assert await repository.delete(hero.id, expected_version=None) is True
        assert await repository.delete(hero.id, expected_version=None) is False
        assert await repository.update(hero.id, {"name": "B"}, expected_version=None) is None


async def test_scope_restricts_single_record_writes() -> None:
    """A record outside `scope` is not found (None/False), even with the right version."""
    async with _committed_hero(owner="alice") as hero:
        bob = [FilterClause("owner_id", FilterOp.EQ, "bob")]
        alice = [FilterClause("owner_id", FilterOp.EQ, "alice")]
        async with async_session_factory() as session:
            repository = SQLAlchemyRepository(session, Hero)
            version = hero.updated_at
            assert (
                await repository.update(hero.id, {"name": "X"}, expected_version=version, scope=bob)
                is None
            )
            assert await repository.delete(hero.id, expected_version=version, scope=bob) is False
            assert await repository.update(
                hero.id, {"name": "X"}, expected_version=version, scope=alice
            )


async def test_locked_record_is_refused_after_the_version_check() -> None:
    """A stale version is a 412-type error even on a locked record; a current one is locked."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        locked = await repository.update(hero.id, {"is_locked": True}, expected_version=None)
        assert locked is not None
        with pytest.raises(PreconditionFailedError):
            await repository.update(hero.id, {"name": "X"}, expected_version=hero.updated_at)
        with pytest.raises(RecordLockedError):
            await repository.update(hero.id, {"name": "X"}, expected_version=locked.updated_at)


async def test_hard_delete_is_conditional_too() -> None:
    """Without `archived_at`, delete() is a real DELETE ... WHERE version, with the same rules."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _hard_table.create(sync_conn, checkfirst=True))
    try:
        async with async_session_factory() as session:
            repository = SQLAlchemyRepository(session, _HardRecord)
            record = await repository.create({"value": 1})
            await session.commit()
            with pytest.raises(PreconditionFailedError):
                await repository.delete(
                    record.id, expected_version=record.updated_at - timedelta(seconds=1)
                )
            assert await repository.delete(record.id, expected_version=record.updated_at)
            assert await repository.get(record.id) is None
            assert await repository.delete(record.id, expected_version=None) is False
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_hard_table.drop)
