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
  `build_web_router` and `build_resource_router`; `crud_manifest_router` and
  `api_catalog_router` (`controllers.crud_manifest`).
- `craftainer_crud.manifest`: `ManifestRegistry`, `ManifestOptions` and
  `build_manifest`, the CRUD manifest builder.

## CRUD manifest

A server describes its resources, their filters, optional routes and cache hints in
one JSON document ([`docs/manifest.md`](docs/manifest.md), schema
[`schemas/crud-manifest/v1.json`](schemas/crud-manifest/v1.json)). Wire it up once:

```python
from craftainer_crud.controllers.crud_manifest import api_catalog_router, crud_manifest_router
from craftainer_crud.controllers.crud_router import build_resource_router
from craftainer_crud.manifest import ManifestOptions, ManifestRegistry

registry = ManifestRegistry()

router = build_resource_router(
    ...,  # as before
    manifest=ManifestOptions(
        registry=registry,
        name="heroes",
        api_version="v2",
        model=HeroModel,
        owner_scoped=True,
        read_roles=["viewer"],
        write_roles=["editor"],
        delete_roles=["maintainer"],
    ),
)
app.include_router(
    crud_manifest_router(
        registry,
        service_name="my-api",
        service_version="1.4.0",
        stats_ttl=30,
        oidc_discovery_url="https://idp/.well-known/openid-configuration",
    )
)  # GET /api/_crud/manifest
app.include_router(
    api_catalog_router(manifest_url="/api/_crud/manifest")
)  # GET /.well-known/api-catalog
```

The factories register what they build, so the manifest cannot list a route that
is not mounted. It is filtered to the caller by running each resource's own
`read_roles` dependency, and carries an `ETag` over that filtered body.

The catalog's `service-desc` link follows the serving app's `openapi_url`
(RFC 9727 makes the link optional): `FastAPI(openapi_url=None)` omits it, a custom
path is advertised as is. Pass `openapi_url="/spec.json"` to override, or
`openapi_url=None` to omit it regardless of the app.

## Event stream subscriber ids

`GET <prefix>/events` hands each subscriber a server-minted, HMAC-signed id in its
first frame and resumes a session only for an id it minted for that resource. The
key is `CrudConfig.subscriber_secret` (at least 32 bytes), random per process when
unset; set the same value on every instance (and keep it across restarts) for
resume to work across workers and restarts. A forged or unknown id is not an error:
the stream starts fresh under a new id.

### Transient and durable subscribers

`MQTTEventSource.subscribe(subscriber_id, *, durable=True)` connects with a persistent
session by default (`clean_session=False`): the broker queues QoS 1 events while the
subscriber is away and replays them when the same id returns. That is what SSE clients
want, and it is all `GET <prefix>/events` ever does; the router does not expose
`durable`.

A long-lived in-process consumer that always starts from scratch (a cache invalidator)
should call `subscribe(None, durable=False)` (a fresh id, never one a durable subscriber uses, since
the clean-session connect would discard that session). It connects with a clean session, so the
broker drops the session when the connection ends, whether it closes cleanly or crashes,
and a restart leaves nothing behind. Nothing is replayed to a transient subscriber.
`InMemoryEventSink.subscribe` accepts the keyword and ignores it.

Production brokers should also set a finite persistent-session expiry, sized to the
longest SSE replay window clients need (for example Mosquitto's
`persistent_client_expiration 1h`). Mosquitto's default never expires persistent
sessions, so abandoned durable SSE sessions would pile up without bound; EMQX defaults
to 2h. The setting is a backstop, not a substitute for transient subscribers.

### Bounding abandoned durable sessions (`session_ttl`)

`MQTTEventSource(..., session_ttl=3600)` (or `MQTTConfig(session_ttl_seconds=3600)`
through `CrudConfig`) makes durable subscribers connect over MQTT 5.0 with Clean Start 0
and a Session Expiry Interval of that many seconds, so a browser SSE client that never
returns has its session and queued events dropped by the broker after the TTL. Within the
TTL a returning id is replayed as before. The value is 1 to 2**32-1 seconds; the default
`None` keeps the 3.1.1 behaviour above. Transient subscribers and the publisher stay on
3.1.1, and v3 and v5 clients interoperate on the same topic. The broker must speak MQTT
5.0 and may cap the value (Mosquitto `max_session_expiry_interval`). See
`docs/adrs/0005-durable-session-ttl.md`.

### Closing an event iterator

`EventSource.subscribe()` connects (or registers a queue) before it returns, so the
iterator it returns, an `EventIterator` (`craftainer_crud.interfaces.base`: an
`AsyncIterator` with `aclose()`), owns a live resource from the start. `aclose()` it when
done, started or not:

```python
import contextlib

subscriber_id, events = await source.subscribe(None)
async with contextlib.aclosing(events):
    async for event in events:
        ...
```

`aclose()` and garbage collection release it; nothing else does. A task cancelled between
`subscribe()` returning and the `async with` is entered is covered only by garbage
collection. See
`docs/adrs/0006-release-unstarted-event-iterators.md`.

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
