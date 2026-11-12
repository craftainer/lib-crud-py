"""Integration: update()/delete() optimistic concurrency against a real Postgres.

Each race commits its rows (two sessions must see each other's writes), so every test
removes what it created in a `finally`, even on failure.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import ColumnElement, Integer, Table
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
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


# --- deterministic interleavings: another writer commits between the read and the write ---


async def _commit_hero_change(hero_id: int, **values: object) -> None:
    """Commit `values` onto a hero from a second session (the "other writer")."""
    async with async_session_factory() as session:
        await session.execute(sa_update(Hero).where(Hero.id == hero_id).values(**values))
        await session.commit()


async def _bump_version(hero_id: int) -> None:
    await _commit_hero_change(hero_id, updated_at=Hero.updated_at + timedelta(seconds=1))


def _interleave(
    repository: SQLAlchemyRepository[Hero],
    monkeypatch: pytest.MonkeyPatch,
    *,
    after_first_read: Callable[[], Awaitable[None]],
    before_second_read: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Run `after_first_read` after the pre-read and `before_second_read` before the re-read."""
    real = repository._writable
    calls = 0

    async def wrapper(
        record_id: int,
        data: dict[str, Any] | None,
        *,
        expected_version: datetime | None,
        scope: Sequence[FilterClause],
    ) -> tuple[Hero | None, Sequence[ColumnElement[bool]]]:
        nonlocal calls
        calls += 1
        call = calls
        if call == 2 and before_second_read is not None:
            await before_second_read()
        result = await real(record_id, data, expected_version=expected_version, scope=scope)
        if call == 1:
            await after_first_read()
        return result

    monkeypatch.setattr(repository, "_writable", wrapper)


async def test_a_version_bump_between_read_and_write_is_a_precondition_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conditional UPDATE/soft-delete matches nothing and the re-read names the mismatch."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(repository, monkeypatch, after_first_read=lambda: _bump_version(hero.id))
        with pytest.raises(PreconditionFailedError):
            await repository.update(hero.id, {"name": "X"}, expected_version=hero.updated_at)
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(repository, monkeypatch, after_first_read=lambda: _bump_version(hero.id))
        with pytest.raises(PreconditionFailedError):
            await repository.delete(hero.id, expected_version=hero.updated_at)


async def test_a_record_removed_between_read_and_write_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the row vanished meanwhile, update() returns None and delete() returns False."""

    async def remove(hero_id: int) -> None:
        async with async_session_factory() as session:
            await session.execute(sa_delete(Hero).where(Hero.id == hero_id))
            await session.commit()

    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(repository, monkeypatch, after_first_read=lambda: remove(hero.id))
        assert (
            await repository.update(hero.id, {"name": "X"}, expected_version=hero.updated_at)
            is None
        )
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(repository, monkeypatch, after_first_read=lambda: remove(hero.id))
        assert await repository.delete(hero.id, expected_version=None) is False


async def test_a_lock_taken_between_read_and_write_blocks_even_an_unconditional_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write's own WHERE excludes a locked row, so expected_version=None can't slip past."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(
            repository,
            monkeypatch,
            after_first_read=lambda: _commit_hero_change(hero.id, is_locked=True),
        )
        with pytest.raises(RecordLockedError):
            await repository.update(hero.id, {"name": "X"}, expected_version=None)
        stored = await _stored(hero.id)
        assert stored is not None
        assert stored.name == "Racer"


async def test_a_change_that_is_undone_before_the_re_read_is_reported_as_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost write whose cause has vanished again is a conflict, not a missing record."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        _interleave(
            repository,
            monkeypatch,
            after_first_read=lambda: _commit_hero_change(hero.id, is_locked=True),
            before_second_read=lambda: _commit_hero_change(hero.id, is_locked=False),
        )
        with pytest.raises(PreconditionFailedError):
            await repository.update(hero.id, {"name": "X"}, expected_version=None)


async def test_versions_are_strictly_increasing_even_inside_one_transaction() -> None:
    """Two writes in one transaction share Postgres's now(); the version still moves each time."""
    async with _committed_hero() as hero, async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        first = await repository.update(hero.id, {"name": "A"}, expected_version=None)
        assert first is not None
        seen = first.updated_at
        second = await repository.update(hero.id, {"name": "B"}, expected_version=seen)
        assert second is not None
        assert second.updated_at > seen
