"""Benchmarks of the crud hot paths: query parsing, in-memory filtering, one router round trip."""

import asyncio
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pytest_benchmark.fixture import BenchmarkFixture
from starlette.datastructures import QueryParams

from craftainer_crud.controllers.crud_query import parse_filters
from craftainer_crud.controllers.crud_router import build_json_router
from craftainer_crud.interfaces.base import CRUDInterface
from craftainer_crud.repositories.filtering import FilterClause, FilterOp, SortClause
from craftainer_crud.repositories.memory import InMemoryRepository
from tests.support.hero_model import Hero
from tests.support.hero_views import HeroV2, HeroV2Create, HeroV2Update

_RECORDS = 500


@pytest.fixture(scope="module")
def runner() -> Iterator[asyncio.Runner]:
    """One event loop for every benchmark in this module."""
    with asyncio.Runner() as loop_runner:
        yield loop_runner


@pytest.fixture(scope="module")
def repository(runner: asyncio.Runner) -> InMemoryRepository[Hero]:
    """An in-memory repository seeded with a fixed set of heroes."""
    repo = InMemoryRepository(Hero)

    async def _seed() -> None:
        for index in range(_RECORDS):
            await repo.create(
                {
                    "name": f"hero-{index:04d}",
                    "powers": ["Flight"],
                    "power_level": index % 100,
                    "owner_id": "bench",
                }
            )

    runner.run(_seed())
    return repo


def test_parse_filters(benchmark: BenchmarkFixture) -> None:
    """Turn a realistic query string into validated FilterClauses."""
    params = QueryParams(
        "name__icontains=hero&power_level__min=10&power_level__max=90&id__in=1,2,3"
    )
    clauses = benchmark(parse_filters, HeroV2, params)
    assert len(clauses) == 4


def test_in_memory_filter_and_sort(
    benchmark: BenchmarkFixture, runner: asyncio.Runner, repository: InMemoryRepository[Hero]
) -> None:
    """Filter and sort the seeded repository, the work behind every mock-mode list."""
    filters = [
        FilterClause("name", FilterOp.ICONTAINS, "hero-00"),
        FilterClause("power_level", FilterOp.GTE, 10),
    ]
    sort = [SortClause("power_level", descending=True)]

    def _list() -> int:
        return len(runner.run(repository.list(filters=filters, sort=sort, limit=50)))

    assert benchmark(_list) == 50


def test_router_list_round_trip(
    benchmark: BenchmarkFixture, repository: InMemoryRepository[Hero]
) -> None:
    """GET a filtered page through a generated JSON router: routing, parsing, serialization."""

    def _get_crud() -> CRUDInterface[HeroV2, Hero]:
        return CRUDInterface(schema=HeroV2, repository=repository)

    crud = Annotated[CRUDInterface[HeroV2, Hero], Depends(_get_crud)]
    no_auth = Depends(lambda: None)
    app = FastAPI()
    app.include_router(
        build_json_router(
            prefix="",
            tags=["heroes"],
            resource_label="Hero",
            schema=HeroV2,
            create_schema=HeroV2Create,
            update_schema=HeroV2Update,
            crud_dependency=crud,
            read_roles=no_auth,
            write_roles=no_auth,
            delete_roles=no_auth,
        ),
        prefix="/heroes",
    )
    with TestClient(app) as client:
        response = benchmark(client.get, "/heroes", params={"power_level__min": 50, "limit": 25})
    assert response.status_code == 200
    assert len(response.json()) == 25
