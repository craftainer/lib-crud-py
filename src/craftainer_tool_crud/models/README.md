# crud/models/

The generic SQLAlchemy base/mixins and the shared revision-history model
every resource's own model (e.g. `app.models.hero`) builds on. Lowest
layer in `crud`'s own import order — never imports from any other
`crud/` subpackage.

- `base.py` — `Base` (the declarative base every model inherits from),
  `IdentifiedBase` (adds the single-column integer `id` primary key
  `crud.repositories`/`crud.interfaces`'s generics are bound to), the async
  `engine`/`async_session_factory`, `get_db` (the FastAPI dependency
  that yields a request-scoped `AsyncSession`, committed on success), and
  `DBSession` (the `Annotated` alias that depends on it at
  `scope="function"`). Depend on `DBSession`, never on `Depends(get_db)`
  directly: at `Depends()`'s default `scope="request"` the commit runs
  after the response is sent, so a client acting on a write's own
  response can fail to see its own write — see the comment on the alias.
- `mixins.py` — opt-in record-lifecycle mixins (`Archivable`/`Draftable`/
  `Schedulable`/`Lockable`), each adding one plain column; a model opts in
  with plain multiple inheritance (`class Hero(IdentifiedBase, Archivable,
  ...)`). See `../repositories/README.md` for how `SQLAlchemyRepository`/
  `InMemoryRepository` detect and act on these, and
  `docs/adrs/0012-soft-delete-via-marker-column.md` for why `Archivable`
  reuses the same row/table rather than a second archive table.
- `revision.py` — `Revision`, the one shared (not per-resource) table
  backing revision history; see `../interfaces/README.md`'s
  `RevisionSink` paragraph.

A resource's own model (e.g. `app.models.hero.Hero`) lives in `app/models/`
instead, not here — see its own `README.md`.

## Do

- Subclass `IdentifiedBase`, not `Base` directly, unless a model
  genuinely doesn't have a single-column integer `id` primary key.
- Add a migration after adding or changing a model: `uv run alembic
  revision --autogenerate -m "..."` — see `../../../alembic/README.md`.

## Don't

- Import from `crud.views`, `crud.repositories`, or `crud.interfaces` —
  see `../README.md`'s "Layering" section.
- Add resource-specific code here — a resource's own model belongs in
  `app/models/` instead, subclassing `IdentifiedBase`/the mixins here.
