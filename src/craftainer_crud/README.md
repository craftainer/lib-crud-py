# craftainer_crud/

The generic CRUD framework a host application's resource packages build on —
router factories, the CRUD interface, storage-agnostic repositories, and
model/view bases — laid out as an MVC-ish split, each subpackage with its own
`README.md`:

- `models/` — the generic SQLAlchemy declarative base, record-lifecycle
  mixins, and shared revision model.
- `views/` — the generic Pydantic view base and bulk/revision/stats
  response shapes.
- `interfaces/` — the generic CRUD interface, built from a view + a
  repository.
- `repositories/` — storage-agnostic CRUD access, backing `interfaces/`.
- `controllers/` — the generic CRUD router factories and the manifest routes.
- `manifest/` — the CRUD manifest: the registry the router factories fill, its
  pydantic models and the builder that renders it from `field_specs` and the
  routers' own limits. Served by `controllers/crud_manifest.py`; contract in
  `docs/manifest.md`.

None of these carry resource-specific code — a resource's own model,
views and router live in the host application and import from here to
build on this framework. `tests/support/` holds a worked example (a
`Hero` model and its views).

## Layering

Import order within `crud/` is strict and one-directional — lower
layers never import from higher ones (`models` → `views` →
`repositories` → `interfaces` → `controllers`) — enforced by
`import-linter`'s `"crud layers"` contract in `../../pyproject.toml`'s
`[tool.importlinter]`, run via `uv run lint-imports` (wired into
`../../.pre-commit-config.yaml`'s manual/pre-push stage, same as mypy).
`manifest/` sits outside that contract: `controllers/crud_router.py` imports its
registry, and its builder imports `controllers/crud_query.py`, so it is a sibling
of `controllers`, not a layer below it (`manifest/registry.py` itself imports
nothing from `controllers`). This contract covers only ordering *within* the package. `interfaces/base.py`
imports `aiomqtt` lazily (`MQTTEventSink`/`MQTTEventSource`) — a
third-party dependency, not another module here, so it doesn't change
this layer order.

This package imports nothing from the host application; the
`"crud never imports app"` import-linter contract enforces it. Its
generic `xml_codec.py` and `web_components.py` live at the package top
level (below `controllers`, which uses them), not in `app`.

```mermaid
graph LR
    models --> views --> repositories --> interfaces --> controllers
```

An arrow means "may import from" — each module may depend on anything
to its left, never anything to its right.

## Host integration

The host application owns settings, the database engine and rate limiting;
this package receives them:

- `config.py` — `CrudConfig` (`use_memory`, `bulk_action_max_matched`,
  `sse_keepalive_seconds`, `mqtt: MQTTConfig`) and `configure(config,
  session_factory)`. Call it once at startup, before the first request:
  `models.base.get_db`/`DBSession` open sessions from the injected
  `async_sessionmaker` (`get_session_factory()` raises if none was
  supplied), and providers/routes read `get_config()` at call time. No
  module here creates an engine or reads environment settings.
- Rate limiting: the router factories (`build_resource_router`,
  `build_json_router`, `build_xml_router`) take an optional
  `bulk_rate_limit` route decorator (e.g. `limiter.limit("20/minute")`)
  for the bulk-action routes; `None` means unlimited. This package does
  not depend on `slowapi`.
- Stats caching: the router factories take an optional `stats_cache`
  (`interfaces/cache.py`'s `StatsCache` protocol) to serve `/stats` and
  `/predict` from a shared cache, and `None` means no caching. The host supplies the
  implementation and drives invalidation from the CRUD event stream; this package
  does not depend on Redis.
- Migrations stay app-owned: the app's Alembic setup targets
  `models.base.Base.metadata`, so a release of this package that changes
  its models (`revision`, mixins) needs a new app-side migration
  (NFR-0019 in the host repository).
- Extras: `[mqtt]` (`aiomqtt`, imported only inside
  `MQTTEventSink`/`MQTTEventSource`) and `[xml]` (`defusedxml`, imported only
  inside `xml_codec.from_xml` and the router's XML-body parsing).
  `tests/unit/test_optional_extras.py` proves the core imports without them.

## Do

- Add a new generic, resource-agnostic CRUD capability here (a new
  router factory option, repository method, or view shape usable by any
  future resource) — see each subpackage's own `README.md` for where it
  belongs.

## Don't

- Add resource-specific code anywhere in this package — a resource's
  own model/view/router belongs in the host application, built on top of the
  generic pieces here.
- Import anything from the host application (`app`) — pass what you need in
  through `config.configure(...)` or a factory parameter instead; see "Host
  integration" above.
