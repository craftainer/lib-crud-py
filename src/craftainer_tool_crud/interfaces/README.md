# crud/interfaces/

The generic CRUD interface: feed it a view (a resource's own, e.g.
`app.views.hero_v2`, built on `crud.views.base.ORMView`) and a
repository (`crud.repositories`) to persist it through, and it exposes
`get`/`list`/`create`/`update`/`delete`/`count`/`update_many`/
`delete_many` in terms of that view — a new resource never needs its own
CRUD class, only a model, a view, and a router that wires the two through
`CRUDInterface`.

- `base.py` — `CRUDInterface[SchemaT, ModelT]`, and the `CRUDLike[SchemaT]`
  `Protocol` describing its full method shape. `CRUDInterface` converts to/
  from the backing ORM model entirely via the view's own `from_attributes`
  support (see `crud.views.base.ORMView`) — this module has no
  resource-specific code and imports nothing from `crud.views` or
  `crud.repositories` beyond their base types. `list`/`update_many`/
  `delete_many`/`count` take `filters`/`sort` sequences from
  `crud.repositories.filtering`, passed straight through to the repository —
  see `crud.controllers.crud_query` for where those come from on an actual
  request. `CompatCRUD` (below) satisfies `CRUDLike` too, structurally —
  `../controllers/crud_router.py`'s router factories are written against
  `CRUDLike` rather than concretely against `CRUDInterface`, specifically so
  the same factory builds both a current-version router and a deprecated
  one. `base.py` also has `OwnerScope`, an opt-in per-user/per-tenant
  scoping hook: pass `CRUDInterface(..., owner=OwnerScope(field, value))` and
  `update`/`delete`/`update_many`/`delete_many` are restricted to records
  where `field == value`, `create` stamps `field` with `value` rather than
  trusting it from the input view, and `get`/`list`/`count` are restricted
  too *unless* `read_scoped=False` is also passed, in which case every
  caller reads every record but can still only write their own.
  `owner=None` (the default) changes nothing — a resource that never passes
  it is unaffected. Resolve `value` from the caller's claims (typically
  `claims["sub"]`) the same way `get_hero_crud` resolves its repository: as
  a `Depends(get_current_claims)` parameter of the resource's own
  `get_<resource>_crud`, not a new mechanism — see `app.crud_1.heroes.
  heroes_v2.get_hero_crud` for the worked example (Hero uses
  `read_scoped=False`: every caller reads every hero, same as before this
  was added, but can only update/delete their own),
  `docs/adrs/0011-owner-scoped-crud-example-resource.md` for the full
  rationale, and `tests/unit/crud/interfaces/test_base.py`'s `test_owner_*`/
  `test_read_scoped_*` tests for the scoped, unscoped, and open-read paths.
  `get`/`list`/`count` also take `include_archived`/`include_unpublished`,
  passed straight through to the repository — see `../repositories/
  README.md`'s "Record-lifecycle mixins".

  `CRUDInterface.stats(*, numeric_fields, categorical_fields, filters=,
  bucket=, include_archived=, include_unpublished=)` is a thin
  pass-through to `self._repository.stats(...)` alongside `count` —
  applying the same `_read_scoped` owner restriction `get`/`list`/`count`
  already apply (a no-op when `owner` isn't set, or when `owner.
  read_scoped` is `False`). A generic, read-only operation like `count`,
  not resource-specific, so it lives on `CRUDInterface` itself rather than
  in a resource's own controller — see this section's "Do"/"Don't" below.
  See `../repositories/README.md`'s "Statistics" section for what it
  returns and `../controllers/README.md`'s "Generic CRUD router factories"
  for the `GET <prefix>/stats`/`GET <prefix>/predict` routes built on top
  of it.

  `base.py` also has `RevisionSink`, a small `Protocol`
  (`record(*, resource, record_id, action, snapshot, actor)`) and
  `RepositoryRevisionSink`, the concrete adapter every resource that opts
  in actually uses — it wraps a plain `Repository[Revision]` (the same
  `build_repository_provider`/`SQLAlchemyRepository`/`InMemoryRepository`
  machinery every other resource uses, since `crud.models.revision.
  Revision` is just another `IdentifiedBase` model), so no bespoke storage
  class is needed. Pass `CRUDInterface(..., revisions=RepositoryRevisionSink
  (repo), resource="hero", actor=claims["sub"])` to log every successful
  create/update/update_many/delete/delete_many; `revisions=None` (the
  default) changes nothing, the same opt-in shape as `owner`. `actor` is
  resolved from claims the same way `owner`'s `value` is — see
  `app.crud_1.heroes.heroes_v2.get_hero_crud` for the worked example, and
  `crud.controllers.crud_router`'s `/revisions` route for reading it back.

  `restore`/`restore_many` mirror `delete`/`delete_many`'s own shape
  (id-or-filters, single-or-bulk), clearing `archived_at` instead of
  deleting — see `../repositories/README.md`'s `Archivable` paragraph.
  Unlike the five original mutating methods, `restore`/`restore_many`
  don't call `revisions` (the plan they were added under scoped revision
  logging to `create`/`update`/`update_many`/`delete`/`delete_many` only).

  `base.py` also has `EventSink`/`EventSource`, the publish/subscribe
  Protocols backing a resource's opt-in `GET <prefix>/events` Server-Sent
  Events stream — the same opt-in shape as `RevisionSink` above, but
  deliberately broader in scope: `events`, if passed to `CRUDInterface`,
  is called on every create/update/update_many/delete/delete_many **and**
  restore/restore_many (a subscriber watching real-time activity cares
  about a record becoming visible again, unlike the revision log — see
  `EventSink`'s own docstring). `MQTTEventSink`/`MQTTEventSource` are the
  concrete adapters backing the real deployment (QoS 1, a persistent MQTT
  session keyed by a client-supplied `subscriber_id` — see
  `docs/adrs/0015-mqtt-for-crud-events.md` for the full delivery-guarantee
  design and why MQTT was chosen over this app's existing Redis/Valkey
  service); `InMemoryEventSink` is the single `MODE=mock` adapter
  satisfying both Protocols at once (a plain `asyncio.Queue` fan-out per
  resource, best-effort only, no delivery guarantee). Pass
  `CRUDInterface(..., events=EventSink)` and add
  `event_source_dependency=` to `build_json_router`/`build_resource_router`
  (see `../controllers/README.md`'s "Generic CRUD router factories") —
  `events=None` (the default) changes nothing. `crud.interfaces.dependency.
  build_event_sink_provider(resource)`/`build_event_source_provider(resource)`
  choose between the two adapters by `Settings.mode`, the same pattern as
  `build_repository_provider` — see `app.crud_1.heroes.heroes_v2.
  get_hero_crud` for the worked example. `base.py` importing `aiomqtt`
  directly (for those two adapters) is a third-party dependency, not
  another `crud/` module, so it doesn't change this package's own layer
  order below.
- `compat.py` — `CompatCRUD`, a generic wrapper that adapts a current
  `CRUDInterface` to speak in terms of an older (deprecated) API
  version's view, via caller-supplied converter functions. The building
  block for a resource that's grown a deprecated version — see
  `../controllers/README.md`'s "API and model versioning" and
  `docs/adrs/0002-api-and-model-versioning.md`.
- `dependency.py` — `build_repository_provider(model)`, the one genuinely
  duplicated fragment of a resource's `get_<resource>_crud`: choosing an
  `InMemoryRepository` (MODE=mock, built once and shared across requests)
  vs. a request-scoped `SQLAlchemyRepository`. Returns a
  `Callable[[AsyncSession], Repository[ModelT]]` a controller calls with
  its request's session — see `app.crud_1.heroes` for the pattern.
  `build_event_sink_provider(resource)`/`build_event_source_provider(resource)`
  follow the same MODE-branching shape for `EventSink`/`EventSource`
  above: a shared `InMemoryEventSink` under `MODE=mock` (one per resource,
  paired so a sink's `publish()` reaches a source's `subscribe()`-
  registered queues), a fresh, stateless `MQTTEventSink`/`MQTTEventSource`
  otherwise.
  The route-level factories built on top of `CRUDInterface`/`CompatCRUD`
  (`build_json_router`/`build_xml_router`/`build_web_router`) live in
  `../controllers/crud_router.py`, one layer up — see `../controllers/
  README.md`'s "Generic CRUD router factories".

## Do

- Build one `CRUDInterface` per request, in the controller, from the
  concrete view and `build_repository_provider(Model)(session)` — see
  `app.crud_1.heroes.get_hero_crud` for the pattern.

## Don't

- Import from `crud.controllers` — see `../README.md`'s "Layering"
  section.
- Add a resource-specific method to `CRUDInterface` — if a resource
  needs behavior beyond the five generic operations, add it in that
  resource's controller instead, calling `CRUDInterface`/the repository
  it wraps directly.
