# crud/views/

Generic Pydantic view bases and response shapes a resource's own views
(under `app.views`) build on.

- `base.py` — `ORMView`, the base every view inherits from
  (`model_config = ConfigDict(from_attributes=True)`, which is what lets
  `crud.interfaces.base.CRUDInterface` build one straight from a SQLAlchemy
  model instance via `model_validate`), and `IXDTFDatetime` (see below).
- `bulk.py` — `BulkUpdateResult`/`BulkDeleteResult`, the response shape
  for a bulk update/delete action (matched count plus the ids affected).
  Plain `BaseModel` subclasses, not `ORMView`: they wrap an already-
  validated result the controller assembles itself, not a raw ORM
  instance `CRUDInterface` builds one of via `from_attributes`.
- `revision.py` — `RevisionView`, one revision-log entry as returned by a
  resource's `GET <prefix>/revisions` route; see `../interfaces/
  README.md`'s `RevisionSink` paragraph.
- `stats.py` — the response shapes for the generic `GET <prefix>/stats`/
  `GET <prefix>/predict` routes (`NumericFieldStatView`/
  `CategoricalValueCountView`/`TimeBucketCountView`/`LifecycleStatsView`/
  `ResourceStatsView`/`PredictionView`), flat list-of-item sub-models
  rather than a dict-keyed-by-field-name shape — this is what lets
  `crud.controllers.crud_router`'s XML router render each row as its own
  flat model via `app.xml_codec.to_xml` (which only supports scalar/
  flat-list fields) and hand-assemble the wrapping tags, the same
  pattern already used for a list of records.

## IXDTF timestamps

`base.py`'s `IXDTFDatetime` (a `datetime` `Annotated` type) serializes
as an RFC 9557 IXDTF string (`...Z[UTC]` — every stored timestamp is
UTC, see `../models/base.py`'s `IdentifiedBase`'s `created_at`/
`updated_at`); use it on any read view field carrying a timestamp. See
`../../app/README.md`'s "Sunset/Deprecation headers" for the related,
but HTTP-date-formatted, `Sunset` header.

## Do

- Subclass `ORMView`, not `pydantic.BaseModel` directly, for any view
  `crud.interfaces.base.CRUDInterface` will build from an ORM instance.

## Don't

- Import from `crud.repositories`, `crud.interfaces`, or `crud.
  controllers` — see `../README.md`'s "Layering" section. A view
  converts to/from an ORM instance structurally (`from_attributes`),
  never by importing the model class.
- Add a resource's own view here — that belongs in `app/views/` instead,
  building on `ORMView`/`bulk.py`/`revision.py`/`stats.py` here.
