"""Integration test: SQLAlchemyRepository against the real Postgres stack service."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Integer, Table
from sqlalchemy.orm import Mapped, mapped_column

from app.models.hero import Hero
from crud.models.base import IdentifiedBase, async_session_factory, engine
from crud.repositories.base import RecordLockedError
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.sqlalchemy import SQLAlchemyRepository
from crud.repositories.stats import TimeBucket


class _PlainRecord(IdentifiedBase):
    """A model with none of crud.models.mixins' record-lifecycle mixins.

    Only used by test_stats_lifecycle_is_none_without_any_mixin below, to exercise
    SQLAlchemyRepository._lifecycle_stats's "no mixin present at all" branch
    (`return None`) against a real query -- Hero, this module's only other bound
    model, always carries every mixin. The table is created/dropped around that
    one test so it leaves nothing behind in the shared Postgres service.
    """

    __tablename__ = "stats_test_plain_records_integration"

    value: Mapped[int] = mapped_column(Integer, default=0)


_plain_record_table = cast(Table, _PlainRecord.__table__)


async def test_crud_roundtrip_against_real_postgres() -> None:
    """create/get/list/update/delete all round-trip through a real Postgres session.

    Runs inside one uncommitted session so nothing is left behind afterwards --
    closing the session without committing rolls back everything written here.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)

        created = await repository.create(
            {"name": "Iron Man", "powers": ["Powered armor"], "owner_id": "tester"}
        )
        assert created.id is not None

        fetched = await repository.get(created.id)
        assert fetched is not None
        assert fetched.name == "Iron Man"

        heroes = await repository.list()
        assert any(hero.id == created.id for hero in heroes)

        updated = await repository.update(created.id, {"powers": ["Repulsor blasts"]})
        assert updated is not None
        assert updated.powers == ["Repulsor blasts"]

        # Hero is Archivable (see crud.models.mixins): delete() marks archived_at
        # rather than removing the row, so get() excludes it by default but the
        # row itself is still there -- an archived row is excluded from normal
        # reads, so a second delete() and a plain update() both act as if the
        # row is gone (False/None), same as a genuine hard delete would from the
        # caller's point of view. Only restore()/get(include_archived=True)
        # reach it.
        assert await repository.delete(created.id) is True
        assert await repository.get(created.id) is None
        assert await repository.get(created.id, include_archived=True) is not None
        assert await repository.delete(created.id) is False
        assert await repository.update(created.id, {"powers": ["N/A"]}) is None
        restored = await repository.restore(created.id)
        assert restored is not None
        assert restored.archived_at is None
        assert await repository.get(created.id) is not None


async def test_filter_sort_and_bulk_actions_against_real_postgres() -> None:
    """list/count/update_many/delete_many honor FilterClause/SortClause against Postgres.

    Runs inside one uncommitted session, same isolation as the roundtrip test above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)

        batman = await repository.create(
            {"name": "Batman", "powers": ["Detective skills"], "owner_id": "tester"}
        )
        batgirl = await repository.create(
            {"name": "Batgirl", "powers": ["Detective skills"], "owner_id": "tester"}
        )
        superman = await repository.create(
            {"name": "Superman", "powers": ["Flight"], "owner_id": "tester"}
        )

        name_filter = [FilterClause("name", FilterOp.ICONTAINS, "bat")]
        assert await repository.count(filters=name_filter) == 2

        matching = await repository.list(filters=name_filter, sort=[SortClause("name")])
        assert [hero.name for hero in matching] == ["Batgirl", "Batman"]

        id_filter = [FilterClause("id", FilterOp.IN, [batman.id, batgirl.id, superman.id])]
        descending = await repository.list(
            filters=id_filter, sort=[SortClause("name", descending=True)]
        )
        assert [hero.name for hero in descending] == ["Superman", "Batman", "Batgirl"]

        updated = await repository.update_many(
            filters=name_filter, data={"powers": ["Martial arts"]}
        )
        assert {hero.id for hero in updated} == {batman.id, batgirl.id}
        assert all(hero.powers == ["Martial arts"] for hero in updated)

        deleted = await repository.delete_many(filters=name_filter)
        assert {hero.id for hero in deleted} == {batman.id, batgirl.id}
        assert await repository.get(batman.id) is None
        assert await repository.get(superman.id) is not None


async def test_every_filter_op_against_real_postgres() -> None:
    """Each FilterOp maps to a working predicate in the real Postgres backend.

    Runs inside one uncommitted session, same isolation as the tests above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        low = await repository.create(
            {"name": "FilterOp Low", "powers": ["A"], "owner_id": "tester"}
        )
        high = await repository.create(
            {"name": "FilterOp High", "powers": ["A"], "owner_id": "tester"}
        )
        ids = [low.id, high.id]

        def by(op: FilterOp, value: object) -> list[FilterClause]:
            return [FilterClause("id", FilterOp.IN, ids), FilterClause("id", op, value)]

        assert [h.id for h in await repository.list(filters=by(FilterOp.EQ, low.id))] == [low.id]
        assert {h.id for h in await repository.list(filters=by(FilterOp.NE, low.id))} == {high.id}
        assert [h.id for h in await repository.list(filters=by(FilterOp.LT, high.id))] == [low.id]
        assert {h.id for h in await repository.list(filters=by(FilterOp.LTE, high.id))} == {
            low.id,
            high.id,
        }
        assert [h.id for h in await repository.list(filters=by(FilterOp.GT, low.id))] == [high.id]
        assert {h.id for h in await repository.list(filters=by(FilterOp.GTE, low.id))} == {
            low.id,
            high.id,
        }
        assert {
            h.id
            for h in await repository.list(
                filters=[
                    FilterClause("name", FilterOp.CONTAINS, "FilterOp"),
                    FilterClause("id", FilterOp.IN, ids),
                ]
            )
        } == {low.id, high.id}
        assert [
            h.id
            for h in await repository.list(
                filters=[FilterClause("name", FilterOp.REGEX, "^FilterOp Low$")]
            )
        ] == [low.id]
        assert [
            h.id
            for h in await repository.list(
                filters=[
                    FilterClause("name", FilterOp.REGEX, "^FilterOp"),
                    FilterClause("id", FilterOp.IN, ids),
                ]
            )
        ] == ids


async def test_lock_blocks_update_and_delete_except_the_unlocking_update() -> None:
    """A locked Hero refuses update/delete (single and bulk), except an update whose
    data is exactly `{"is_locked": False}` -- see
    crud.repositories.base.RecordLockedError and
    crud.repositories.sqlalchemy._raise_if_locked's own docstring.

    Runs inside one uncommitted session, same isolation as the tests above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        locked = await repository.create(
            {
                "name": "Locked Hero",
                "powers": ["Immovable"],
                "owner_id": "tester",
                "is_locked": True,
            }
        )

        with pytest.raises(RecordLockedError):
            await repository.update(locked.id, {"powers": ["Should not apply"]})
        with pytest.raises(RecordLockedError):
            await repository.delete(locked.id)

        id_filter = [FilterClause("id", FilterOp.EQ, locked.id)]
        with pytest.raises(RecordLockedError):
            await repository.update_many(filters=id_filter, data={"powers": ["Nope"]})
        with pytest.raises(RecordLockedError):
            await repository.delete_many(filters=id_filter)

        # Unlocking and editing in the same request is refused too -- unlock must be
        # its own request, not a way to slip an edit past the lock.
        with pytest.raises(RecordLockedError):
            await repository.update(locked.id, {"is_locked": False, "powers": ["Should not apply"]})

        unlocked = await repository.update(locked.id, {"is_locked": False})
        assert unlocked is not None
        assert unlocked.is_locked is False

        edited = await repository.update(locked.id, {"powers": ["Freed"]})
        assert edited is not None
        assert edited.powers == ["Freed"]


async def test_schedulable_visibility_excludes_future_publish_and_past_unpublish() -> None:
    """get()/list() exclude a not-yet-published or no-longer-published Hero by default,
    and `include_unpublished=True` reaches it -- see crud.models.mixins.Schedulable.

    Runs inside one uncommitted session, same isolation as the tests above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        now = datetime.now(UTC).replace(tzinfo=None)

        not_yet_published = await repository.create(
            {
                "name": "Not Yet Published",
                "powers": ["A"],
                "owner_id": "tester",
                "publish_at": now + timedelta(days=1),
            }
        )
        no_longer_published = await repository.create(
            {
                "name": "No Longer Published",
                "powers": ["A"],
                "owner_id": "tester",
                "unpublish_at": now - timedelta(days=1),
            }
        )

        assert await repository.get(not_yet_published.id) is None
        assert await repository.get(no_longer_published.id) is None
        assert await repository.get(not_yet_published.id, include_unpublished=True) is not None
        assert await repository.get(no_longer_published.id, include_unpublished=True) is not None

        ids = [not_yet_published.id, no_longer_published.id]
        id_filter = [FilterClause("id", FilterOp.IN, ids)]
        assert await repository.list(filters=id_filter) == []
        visible = await repository.list(filters=id_filter, include_unpublished=True)
        assert {hero.id for hero in visible} == set(ids)


async def test_restore_many_via_filters() -> None:
    """restore_many() clears archived_at on every matching row.

    Runs inside one uncommitted session, same isolation as the tests above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        batman = await repository.create(
            {"name": "Restorable Batman", "powers": ["A"], "owner_id": "tester"}
        )
        batgirl = await repository.create(
            {"name": "Restorable Batgirl", "powers": ["A"], "owner_id": "tester"}
        )
        assert await repository.delete(batman.id) is True
        assert await repository.delete(batgirl.id) is True

        name_filter = [FilterClause("name", FilterOp.ICONTAINS, "Restorable")]
        restored = await repository.restore_many(filters=name_filter)
        assert {hero.id for hero in restored} == {batman.id, batgirl.id}
        assert all(hero.archived_at is None for hero in restored)
        assert await repository.get(batman.id) is not None


async def test_delete_missing_returns_false() -> None:
    """delete() returns False for an id that doesn't exist, not just an already-archived one."""
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        assert await repository.delete(-1) is False


async def test_restore_single_record_clears_archived_at() -> None:
    """restore() (single-record, not restore_many) clears archived_at and returns the record.

    Runs inside one uncommitted session, same isolation as the tests above.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        assert await repository.restore(-1) is None

        created = await repository.create(
            {"name": "Restore Single", "powers": ["A"], "owner_id": "tester"}
        )
        assert await repository.delete(created.id) is True

        restored = await repository.restore(created.id)
        assert restored is not None
        assert restored.archived_at is None
        assert await repository.get(created.id) is not None


async def test_stats_against_real_postgres() -> None:
    """stats() computes count/numeric/categorical/time-series/lifecycle via real SQL.

    Runs inside one uncommitted session, same isolation as the tests above. Scoped
    to this test's own ids throughout (via an id__in filter), the same defensive
    pattern test_every_filter_op_against_real_postgres already uses, so a
    concurrently-running test's own uncommitted rows can never affect the count.
    """
    async with async_session_factory() as session:
        repository = SQLAlchemyRepository(session, Hero)
        batman = await repository.create(
            {"name": "Stats Batman", "powers": ["A"], "owner_id": "tester"}
        )
        batgirl = await repository.create(
            {
                "name": "Stats Batgirl",
                "powers": ["A"],
                "owner_id": "tester",
                "is_locked": True,
            }
        )
        ids = [batman.id, batgirl.id]
        id_filter = [FilterClause("id", FilterOp.IN, ids)]

        result = await repository.stats(
            numeric_fields=["id"], categorical_fields=["is_draft", "is_locked"], filters=id_filter
        )
        assert result.total == 2
        assert result.numeric["id"].count == 2
        assert result.numeric["id"].minimum == float(min(ids))
        assert result.numeric["id"].maximum == float(max(ids))
        assert result.categorical["is_locked"] == {"False": 1, "True": 1}
        assert result.time_series is None
        assert result.lifecycle is not None
        assert result.lifecycle.locked == 1
        assert result.lifecycle.archived == 0

        with_bucket = await repository.stats(
            numeric_fields=[], categorical_fields=[], filters=id_filter, bucket=TimeBucket.DAY
        )
        assert with_bucket.time_series is not None
        assert sum(bucket.count for bucket in with_bucket.time_series) == 2

        await repository.delete(batman.id)
        after_delete = await repository.stats(
            numeric_fields=[], categorical_fields=[], filters=id_filter
        )
        assert after_delete.total == 1
        assert after_delete.lifecycle is not None
        assert after_delete.lifecycle.archived == 0  # excluded by default, same as list/count

        including_archived = await repository.stats(
            numeric_fields=[], categorical_fields=[], filters=id_filter, include_archived=True
        )
        assert including_archived.total == 2
        assert including_archived.lifecycle is not None
        assert including_archived.lifecycle.archived == 1


async def test_stats_lifecycle_is_none_without_any_mixin_against_real_postgres() -> None:
    """stats() returns `lifecycle=None` for a model with none of the record-lifecycle
    mixins, against a real (throwaway) Postgres table -- see _PlainRecord's own docstring.
    """
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: _plain_record_table.create(sync_conn, checkfirst=True)
        )
    try:
        async with async_session_factory() as session:
            repository = SQLAlchemyRepository(session, _PlainRecord)
            await repository.create({"value": 1})
            result = await repository.stats(numeric_fields=["value"], categorical_fields=[])
            assert result.lifecycle is None
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_plain_record_table.drop)
