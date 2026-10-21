# tests/

Three independent suites:

- `unit/` - no external services; fakes and the in-memory repository stand in
  for the database and the broker.
- `integration/` - `SQLAlchemyRepository` against a real Postgres, read from
  `POSTGRES_HOST/PORT/USER/PASSWORD/DB` (the devcontainer's compose service
  sets them; without them every test skips with a message naming the missing
  variables). The suite creates the `heroes` table in that database and drops
  it afterwards when it created it, so point `POSTGRES_DB` at a dedicated
  database, never one holding real tables.
- `bench/` - `pytest-benchmark` microbenchmarks (query parsing, in-memory
  filtering, one generated-router round trip), excluded from the default run
  (`--ignore=tests/bench` in `addopts`). Run locally with
  `uv run pytest tests/bench --no-cov`, or under CodSpeed with
  `uv run pytest tests/bench --no-cov --codspeed`.

A plain `uv run pytest` collects `unit/` and `integration/` and enforces 100%
line and branch coverage of `src/craftainer_crud`
(`[tool.coverage.report]` in `pyproject.toml`). `SQLAlchemyRepository` is only
covered by `integration/` (its SQL is Postgres-specific), so the gate needs the
database; `checks.yml` runs inside the devcontainer, where the compose stack
provides it.

`support/` holds the package-local test resource (a `Hero` model and its
views) that stands in for a host application's own.

Test code is type-checked under the same `mypy --strict` settings as `src/`
(the hook runs `uv run mypy src tests`), so tests carry full annotations too.
