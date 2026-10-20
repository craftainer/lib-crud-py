# craftainer-crud

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

A generic CRUD framework for [FastAPI](https://fastapi.tiangolo.com/): router
factories (JSON, XML, and a small web form/component set), a CRUD interface,
storage-agnostic repositories (SQLAlchemy async and in-memory), and the model
and view bases they build on. A host application keeps its own models, views
and settings and builds each resource on top of the generic pieces here; this
package imports nothing from the host.

It is an instance of
[`template-python-package`](https://github.com/craftainer/template-python-package)
(so it gets the devcontainer, CI, release workflow and template sync from
there); see [`docs/TEMPLATE.md`](docs/TEMPLATE.md) for the conventions shared
by every instance. The package's own layout and rules are in
[`src/craftainer_crud/README.md`](src/craftainer_crud/README.md).

## Install

```bash
pip install craftainer-crud            # core
pip install "craftainer-crud[mqtt]"    # + MQTT event sink/source (aiomqtt)
pip install "craftainer-crud[xml]"     # + XML request bodies (defusedxml)
```

> [!NOTE]
> Renamed from `craftainer-tool-crud` (import `craftainer_tool_crud`); the
> first release under the new name is `0.2.0`. Install `craftainer-crud` and
> import `craftainer_crud`. See
> [ADR 0001](docs/adrs/0001-rename-package-to-craftainer-crud.md).

The core needs Python 3.14 or newer. Both extras are imported lazily, only
where they are used (`MQTTEventSink`/`MQTTEventSource`, and the XML codec and
XML router's body parsing), so the core imports without them and a missing
extra surfaces as an `ImportError` at first use. Bring your own database
driver (for example `asyncpg`) for `SQLAlchemyRepository`.

## Public API overview

- `craftainer_crud.config`: `CrudConfig`, `MQTTConfig`, `configure(config,
  session_factory)`. Call `configure` once at startup, before the first request.
- `craftainer_crud.models`: the declarative `Base`/`IdentifiedBase`, the
  record-lifecycle mixins (`Archivable`, `Draftable`, `Schedulable`,
  `Lockable`) and the shared `Revision` model.
- `craftainer_crud.views`: the Pydantic view base (`ORMView`), bulk,
  revision and stats response shapes.
- `craftainer_crud.repositories`: the `Repository` protocol,
  `SQLAlchemyRepository`, `InMemoryRepository` and the filter/sort clauses.
- `craftainer_crud.interfaces`: `CRUDInterface`, `OwnerScope`, revision
  and event sinks, `build_repository_provider` and friends.
- `craftainer_crud.controllers`: `build_json_router`, `build_xml_router`,
  `build_web_router` and `build_resource_router`.

## Model changes need an app-side migration

The package ships SQLAlchemy models (`Revision`, the lifecycle mixins) but no
migrations: the host's Alembic setup targets `models.base.Base.metadata`. A
release that changes those models therefore needs a new migration in the host
application, and the release notes say so. Migrations stay immutable once
applied (NFR-0019 in the host repository).

## Versioning

[SemVer](https://semver.org/), starting at `0.1.0`. While the major version is
`0`, a minor release may break the public API; pin `craftainer-crud>=0.x,<0.(x+1)`
until `1.0.0`. The version lives only in the git tag `release.yml` computes;
`pyproject.toml` stays at `0.0.0` and `make build` stamps `RELEASE_VERSION`
into the build. Releases go to TestPyPI first, then PyPI, through trusted
publishing (`PYPI_PUBLISH` repository variable: `testpypi` or `pypi`; unset
publishes nothing).

## Development

Open the repository in the devcontainer (it starts a Postgres for the
integration tests), then run `prek run --all-files --hook-stage manual`. The
`pytest` hook covers `tests/unit` and `tests/integration` under one 100%
coverage gate; see [`tests/README.md`](tests/README.md).

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
