"""Generic FastAPI router factories for a resource's CRUD endpoints.

Parameterized by a resource's Pydantic views and an already-built CRUD
dependency (craftainer_tool_crud.interfaces.base.CRUDLike) -- covers both a current version
(CRUDInterface) and a deprecated one (CompatCRUD) identically, since
both satisfy CRUDLike structurally. Each factory builds its route
functions internally as closures over its arguments: the generated
functions' `crud`/`record` parameters are annotated with a TypeVar-bound
runtime type, which mypy cannot verify statically -- each such line
carries a narrow `# type: ignore[valid-type]`. This factory's own
signature (every parameter, `-> APIRouter`) stays fully strict-typed,
so a caller gets real type-checking on the call itself.

Record addressing is a query parameter (`?id=`), not a path segment, on every
factory here -- see craftainer_tool_crud.controllers.crud_actions for the shared "id present ->
single record; otherwise -> filtered list, or a bulk update/delete over the
given filters" logic each factory's routes wrap in their own response format.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError

from craftainer_tool_crud.config import get_config
from craftainer_tool_crud.controllers import crud_stats
from craftainer_tool_crud.controllers.crud_actions import (
    resolve_delete,
    resolve_list_or_get,
    resolve_restore,
    resolve_update,
)
from craftainer_tool_crud.controllers.crud_query import (
    FieldFilterInfo,
    describe_fields,
    parse_include_archived,
    parse_include_unpublished,
)
from craftainer_tool_crud.interfaces.cache import StatsCache
from craftainer_tool_crud.repositories.filtering import FilterClause, FilterOp, SortClause
from craftainer_tool_crud.repositories.stats import TimeBucket
from craftainer_tool_crud.views.bulk import BulkDeleteResult, BulkUpdateResult
from craftainer_tool_crud.views.revision import RevisionView
from craftainer_tool_crud.views.stats import (
    CategoricalValueCountView,
    LifecycleStatsView,
    NumericFieldStatView,
    PredictionView,
    ResourceStatsView,
    SeriesPointView,
    TimeBucketCountView,
)
from craftainer_tool_crud.web_components import render_crud_component_js, render_crud_form
from craftainer_tool_crud.xml_codec import from_xml, is_list_annotation, to_xml

# A route decorator the host supplies for bulk actions (e.g. slowapi's `limiter.limit(...)`),
# so this package doesn't depend on any rate-limiting library. None means unlimited.
RateLimit = Callable[[Callable[..., Any]], Callable[..., Any]]


def _rate_limited(bulk_rate_limit: RateLimit | None) -> RateLimit:
    """Return `bulk_rate_limit`, or an identity decorator when none was given."""
    return bulk_rate_limit if bulk_rate_limit is not None else lambda func: func


# Unbounded `?limit=` would let a caller pull an entire table in one response --
# cap it the same way skip=0 already bounds the low end.
_MAX_LIMIT = 1000

# Version of these factories' own route shape/behavior -- not a resource's shape.
# Bump only when build_json_router/build_xml_router/build_web_router themselves
# change in a breaking way; every existing resource-version's path still names it
# explicitly (see build_resource_router and docs/adrs/0009-...md) rather than
# hardcoding "v1" independently per resource.
ROUTER_VERSION = 1


class _PublishFlip(BaseModel):
    """Minimal payload flipping `is_draft` off, for `POST <prefix>/publish?id=`.

    A resource's own `*Update` view (see e.g. app.views.hero_v2.HeroV2Update)
    deliberately never exposes `is_draft` for a client to set directly through
    the normal PATCH route -- publishing is a distinct, server-controlled
    action. `CRUDInterface.update`'s `data.model_dump(exclude_unset=True)` only
    needs *some* BaseModel exposing the field, not one tied to a resource's own
    create/update schema, so one small shared model here covers every resource.
    """

    is_draft: bool = False


def _with_dependency_headers[ResponseT: Response](
    response: Response, built: ResponseT
) -> ResponseT:
    """Copy headers a router-level dependency set on the shared `response` onto `built`.

    FastAPI merges a dependency's `response.headers` mutations into the framework's
    own auto-built Response, but not into one a route handler constructs and returns
    itself -- every build_xml_router/build_web_router route does that (XML/redirect/
    JS bodies), so a router-level header dependency like app.http_headers.sunset(...)
    would otherwise have no visible effect on any of them.
    """
    built.headers.raw.extend(response.headers.raw)
    return built


class _PredictionMetaXML(BaseModel):
    """Flat scalar sub-model for build_xml_router's hand-assembled `/predict` body.

    `field`/`bucket`/`method` are the only scalar (non-nested) fields of
    craftainer_tool_crud.views.stats.PredictionView -- rendered as their own flat model via
    craftainer_tool_crud.xml_codec.to_xml the same way a numeric-field/categorical-value/
    time-bucket row is (see build_xml_router's `/stats`/`/predict` routes).
    """

    field: str | None
    bucket: str
    method: str


def _validate_publish_ready(record: BaseModel, create_schema: type[BaseModel]) -> None:
    """Re-validate `record` against `create_schema`, raising 422 naming any field still missing.

    Shared by build_json_router's and build_xml_router's own `/publish` routes --
    see build_json_router's docstring for what this guards against.
    """
    required_fields = {field: getattr(record, field) for field in create_schema.model_fields}
    try:
        create_schema.model_validate(required_fields)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


# `Cache-Control` on every ETag-bearing GET: a browser may keep the response but must
# revalidate it, and a shared proxy never stores it (responses are per-user, ADR-0011).
_REVALIDATE_CACHE_CONTROL = "private, no-cache"

_ETAG_TOKEN = re.compile(r'(?:W/)?"[^"]*"|\*')


def _digest(source: str) -> str:
    """Return a short stable hex digest of `source`, for an entity-tag's opaque value."""
    return hashlib.sha256(source.encode()).hexdigest()[:32]


def _record_version(record: BaseModel) -> str:
    """Return what identifies one record's current state: `id` + `updated_at`.

    A view with no `updated_at` (not every resource exposes one) falls back to its own
    serialized form, which changes whenever anything visible about the record does.
    """
    updated_at = getattr(record, "updated_at", None)
    # pragma: no cover below is for tests/e2e: every e2e resource view has updated_at;
    # tests/unit/craftainer_tool_crud/controllers/test_crud_router_conditional.py covers it.
    if updated_at is None:  # pragma: no cover
        return record.model_dump_json()
    return f"{getattr(record, 'id', '')}:{updated_at.isoformat()}"


def record_etag(record: BaseModel) -> str:
    """Return the strong `ETag` for a single record (shared by GET, PATCH and DELETE)."""
    return f'"{_digest(_record_version(record))}"'


def list_etag(records: Sequence[BaseModel]) -> str:
    """Return the weak `ETag` for one page of records: their ids and `updated_at` values."""
    return f'W/"{_digest("|".join(_record_version(r) for r in records))}"'


def _revalidation_headers(result: BaseModel | Sequence[BaseModel]) -> dict[str, str]:
    """Return the `ETag` + `Cache-Control` headers for a single record or a page of them."""
    etag = record_etag(result) if isinstance(result, BaseModel) else list_etag(result)
    return {"ETag": etag, "Cache-Control": _REVALIDATE_CACHE_CONTROL}


def _entity_tags(header: str) -> list[str]:
    """Split an `If-Match`/`If-None-Match` value into its entity-tags (and `*`)."""
    return _ETAG_TOKEN.findall(header)


def _opaque(tag: str) -> str:
    """Return `tag` without its weakness prefix, for weak comparison."""
    return tag.removeprefix("W/")


def _not_modified(request: Request, response: Response, headers: dict[str, str]) -> Response | None:
    """Return a bodiless `304` if the request's `If-None-Match` matches `headers["ETag"]`.

    Weak comparison (RFC 9110 8.8.3.2): the `W/` prefix is ignored on both sides. `None`
    means "send the full response"; the caller then attaches `headers` to it.
    """
    header = request.headers.get("if-none-match")
    if header is None:
        return None
    etag = _opaque(headers["ETag"])
    if not any(tag == "*" or _opaque(tag) == etag for tag in _entity_tags(header)):
        return None
    return _with_dependency_headers(response, Response(status_code=304, headers=headers))


async def _enforce_if_match(
    crud: Any,
    request: Request,
    *,
    id: int | None,  # noqa: A002
    not_found: str,
) -> None:
    """Raise `412` when `If-Match` is present and the addressed record's `ETag` isn't listed.

    Only a single-record (`?id=`) write is conditional: a bulk action has no one
    representation to compare against, so `If-Match` is ignored there. Strong
    comparison (RFC 9110 8.8.3.2): a weak tag never matches, `*` matches any existing
    record. A missing record falls through to the write's own `404`. The check and the
    write are separate statements, so two writers racing inside that gap can still both
    pass; this closes the read-modify-write window a client holds open, not that one.
    """
    header = request.headers.get("if-match")
    if header is None or id is None:
        return
    # Same visibility as the write itself (crud_actions reads these query params too),
    # so a record the write would 404 on (e.g. archived by a DELETE) also 404s here.
    current = await crud.get(
        id,
        include_archived=parse_include_archived(request.query_params),
        include_unpublished=parse_include_unpublished(request.query_params),
    )
    if current is None:
        return
    etag = record_etag(current)
    if not any(tag in ("*", etag) for tag in _entity_tags(header)):
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED,
            f"{not_found.removesuffix(' not found')} was modified since the supplied If-Match",
        )


async def _resolve_stats(crud: Any, schema: type[BaseModel], request: Request) -> ResourceStatsView:
    """Parse `/stats`'s query params, call CRUDLike.stats, and build its response view.

    Shared by build_json_router's and build_xml_router's own `/stats` routes --
    only the rendering (a plain view vs. hand-assembled XML) differs between them.
    """
    bucket = crud_stats.parse_bucket(request.query_params.get("bucket"))
    include_archived = parse_include_archived(request.query_params)
    include_unpublished = parse_include_unpublished(request.query_params)
    result = await crud.stats(
        numeric_fields=crud_stats.numeric_fields(schema),
        categorical_fields=crud_stats.categorical_fields(schema),
        bucket=bucket,
        include_archived=include_archived,
        include_unpublished=include_unpublished,
    )
    return ResourceStatsView(
        total=result.total,
        numeric=[
            NumericFieldStatView(
                field=field,
                count=field_stats.count,
                minimum=field_stats.minimum,
                maximum=field_stats.maximum,
                average=field_stats.average,
                total=field_stats.total,
            )
            for field, field_stats in result.numeric.items()
        ],
        categorical=[
            CategoricalValueCountView(field=field, value=value, count=count)
            for field, distribution in result.categorical.items()
            for value, count in distribution.items()
        ],
        time_series=(
            None
            if result.time_series is None
            else [
                TimeBucketCountView(
                    bucket_start=datetime.fromisoformat(item.bucket_start), count=item.count
                )
                for item in result.time_series
            ]
        ),
        lifecycle=(
            None
            if result.lifecycle is None
            else LifecycleStatsView(
                archived=result.lifecycle.archived,
                draft=result.lifecycle.draft,
                locked=result.lifecycle.locked,
                scheduled_pending=result.lifecycle.scheduled_pending,
                scheduled_expired=result.lifecycle.scheduled_expired,
            )
        ),
    )


async def _resolve_predict(
    crud: Any,
    schema: type[BaseModel],
    *,
    field: str | None,
    periods: int,
    bucket_raw: str,
) -> PredictionView:
    """Parse `/predict`'s query params, forecast a trend, and build its response view.

    Shared by build_json_router's and build_xml_router's own `/predict` routes --
    only the rendering differs between them. Raises RequestValidationError (422)
    for an unrecognized `field`/`bucket` or fewer than 2 buckets of history --
    see craftainer_tool_crud.controllers.crud_stats.forecast's own docstring for the latter.
    """
    bucket = crud_stats.parse_bucket(bucket_raw) or TimeBucket.DAY
    validated_field = crud_stats.parse_predict_field(schema, field)
    if validated_field is None:
        result = await crud.stats(numeric_fields=(), categorical_fields=(), bucket=bucket)
        series = crud_stats.count_series_as_values(result.time_series or [])
    else:
        records = await crud.list(
            limit=crud_stats.MAX_HISTORY_RECORDS, sort=[SortClause("created_at")]
        )
        series = crud_stats.bucket_field_sums(records, validated_field, bucket)
    try:
        predictions = crud_stats.forecast(series, periods, bucket)
    except crud_stats.InsufficientHistoryError as exc:
        errors = [{"loc": ("query",), "msg": str(exc), "type": "value_error"}]
        raise RequestValidationError(errors) from exc
    last_known = series[-1]
    return PredictionView(
        field=validated_field,
        bucket=bucket.value,
        last_known=SeriesPointView(
            bucket_start=datetime.fromisoformat(last_known.bucket_start), value=last_known.value
        ),
        predictions=[
            SeriesPointView(bucket_start=datetime.fromisoformat(p.bucket_start), value=p.value)
            for p in predictions
        ],
    )


# Response header on a `/stats`/`/predict` response served through a `StatsCache`
# (absent when no cache is configured): "hit" or "miss".
_CACHE_HEADER = "X-Stats-Cache"


def _require_cache_resource(stats_cache: StatsCache | None, resource: str | None) -> str:
    """Return the resource name a `StatsCache` keys its entries by, or raise if it has none.

    Cached entries are invalidated per resource (see `StatsCache.invalidate`), so a
    router given a `stats_cache` must also be given a `resource`. Without a cache the
    name is never used.
    """
    if stats_cache is None:
        return ""
    # e2e-unreachable: a config guard; covered by tests/unit/craftainer_tool_crud/controllers/
    # test_crud_router_stats_cache.py
    if resource is None:  # pragma: no cover
        raise ValueError("stats_cache requires resource= to be set")
    return resource


def _stats_cache_params(request: Request) -> dict[str, Any]:
    """Normalize `/stats`'s query params for its cache key; raises 422 exactly as the route does."""
    bucket = crud_stats.parse_bucket(request.query_params.get("bucket"))
    return {
        "bucket": None if bucket is None else bucket.value,
        "include_archived": parse_include_archived(request.query_params),
        "include_unpublished": parse_include_unpublished(request.query_params),
    }


def _predict_cache_params(
    schema: type[BaseModel], *, field: str | None, periods: int, bucket_raw: str
) -> dict[str, Any]:
    """Normalize `/predict`'s query params for its cache key; raises 422 as the route does."""
    bucket = crud_stats.parse_bucket(bucket_raw) or TimeBucket.DAY
    return {
        "field": crud_stats.parse_predict_field(schema, field),
        "periods": periods,
        "bucket": bucket.value,
    }


async def _cached_body(
    stats_cache: StatsCache,
    resource: str,
    key: str,
    build: Callable[[], Awaitable[bytes]],
) -> tuple[bytes, str]:
    """Return the cached body for `key`, or `build()` and store it; plus "hit"/"miss"."""
    cached = await stats_cache.get(resource, key)
    if cached is not None:
        return cached, "hit"
    body = await build()
    await stats_cache.set(resource, key, body)
    return body, "miss"


def _stats_to_xml(view: ResourceStatsView) -> str:
    """Hand-assemble `ResourceStatsView` as nested XML -- see build_xml_router's `/stats` route.

    `view.lifecycle is None`'s branch below is `# pragma: no cover`: it's populated
    whenever the underlying model carries *any* record-lifecycle mixin (see
    craftainer_tool_crud.repositories.memory/sqlalchemy's own `stats`), and Hero -- the only
    resource wired up through the HTTP layer that tests/e2e exercises -- carries
    every one (Archivable, Draftable, Schedulable, Lockable; see
    app.models.hero.Hero), so `lifecycle` is never actually None through the real
    HTTP stack. tests/unit/controllers/test_crud_router.py exercises the None
    case directly against a hand-built ResourceStatsView -- the pragma only
    affects what's counted toward the e2e coverage gate, not whether this line
    runs there.
    """
    numeric_xml = "".join(to_xml(item, "numeric-field") for item in view.numeric)
    categorical_xml = "".join(to_xml(item, "categorical-value") for item in view.categorical)
    body = (
        f"<total>{view.total}</total>"
        f"<numeric-fields>{numeric_xml}</numeric-fields>"
        f"<categorical-values>{categorical_xml}</categorical-values>"
    )
    if view.time_series is not None:
        time_series_xml = "".join(to_xml(item, "time-bucket") for item in view.time_series)
        body += f"<time-buckets>{time_series_xml}</time-buckets>"
    if view.lifecycle is not None:  # pragma: no cover -- see docstring
        body += to_xml(view.lifecycle, "lifecycle")
    return f"<stats>{body}</stats>"


def _prediction_to_xml(view: PredictionView) -> str:
    """Hand-assemble `PredictionView` as nested XML -- see build_xml_router's `/predict` route."""
    meta = _PredictionMetaXML(field=view.field, bucket=view.bucket, method=view.method)
    predictions_xml = "".join(to_xml(item, "prediction-point") for item in view.predictions)
    body = (
        to_xml(meta, "meta")
        + to_xml(view.last_known, "last-known")
        + f"<predictions>{predictions_xml}</predictions>"
    )
    return f"<prediction>{body}</prediction>"


def _parse_xml_body[ModelT: BaseModel](body: bytes, schema: type[ModelT]) -> ModelT:
    """Parse a request body with from_xml, rejecting a malicious payload with 400.

    defusedxml.ElementTree.fromstring raises a DefusedXmlException (e.g.
    EntitiesForbidden) for a "billion laughs"-style entity-expansion attack --
    without this, that exception would otherwise reach problem_details.py's generic
    500 handler instead of being reported as the client error it is.
    """
    from defusedxml.common import DefusedXmlException  # noqa: PLC0415 -- optional `[xml]` extra

    try:
        return from_xml(body, schema)
    except DefusedXmlException as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed XML body") from exc


def build_json_router[SchemaT: BaseModel, CreateT: BaseModel, UpdateT: BaseModel](
    *,
    prefix: str,
    tags: Sequence[str],
    resource_label: str,
    schema: type[SchemaT],
    create_schema: type[CreateT],
    update_schema: type[UpdateT],
    crud_dependency: Any,  # Annotated[CRUDLike[SchemaT], Depends(...)]
    read_roles: Any,  # a Depends(...) object, e.g. heroes.ReadRoles
    write_roles: Any,
    delete_roles: Any,
    router_dependencies: Sequence[Any] = (),
    draft_schema: Any = None,  # type[BaseModel] | None -- see "/draft"/"/publish" below
    archivable: bool = False,
    revision_repository_dependency: Any = None,  # Annotated[Repository[Revision], Depends(...)]
    resource: str | None = None,
    event_source_dependency: Any = None,  # Annotated[EventSource, Depends(...)]
    stats_enabled: bool = False,
    bulk_rate_limit: RateLimit | None = None,
    stats_cache: StatsCache | None = None,
) -> APIRouter:
    """Build the standard list/create/get/update/delete JSON router for one resource.

    `id`, when present as a query parameter, addresses a single record on the
    GET/PATCH/DELETE routes (404 if missing) -- otherwise GET lists (optionally
    filtered/sorted per craftainer_tool_crud.controllers.crud_query) and PATCH/DELETE act in bulk
    over whatever filters are given, rejecting an empty filter set with no `id`
    (400) so an empty query string can never target every record by accident.

    Single-record GETs carry a strong `ETag` (id + `updated_at`), list GETs a weak one
    over the page, both with `Cache-Control: private, no-cache`; a matching
    `If-None-Match` gets a bodiless `304`. PATCH/DELETE with `?id=` honor `If-Match`
    (`412` problem details on a mismatch) and PATCH returns the new `ETag`. The XML
    router mirrors all of this; the web router serves no records.

    `stats_cache`, when given (with `resource`), serves `/stats` and `/predict` from a
    shared `craftainer_tool_crud.interfaces.cache.StatsCache` as pre-serialized bodies,
    keyed by route, normalized query params, response schema and the caller's
    ownership scope (`crud_stats.cache_scope`); the responses then carry an
    `X-Stats-Cache: hit|miss` header. `None` (the default) leaves both routes exactly
    as they are without a cache.

    `POST <prefix>/clone?id=` is always added -- fully generic, no mixin needed
    (see docs/plans's former "Duplicate/clone" section, now folded into this
    docstring and app/README.md): it builds `create_schema` from the fields it
    itself declares, read off the existing record, so a Draftable/Archivable/
    Schedulable model's own server-assigned fields (`is_draft`, `archived_at`,
    `publish_at`, `unpublish_at` -- none of which `create_schema` ever includes)
    are never copied from the source record.

    `draft_schema` (typically a resource's own `*Update` view, all-optional),
    if given, adds `POST <prefix>/draft` (persists with server-assigned
    lifecycle defaults, e.g. `is_draft=True`) and `POST <prefix>/publish?id=`
    (re-validates the record against `create_schema`, 422 naming any field
    still missing, and flips `is_draft=False` on success).

    `archivable=True` adds `POST <prefix>/restore`, mirroring delete's own
    id-or-filters/single-or-bulk shape.

    `revision_repository_dependency` + `resource` together add
    `GET <prefix>/revisions?id=`, a plain query against the shared Revision
    table (see craftainer_tool_crud.models.revision) for that record's history, newest first --
    not routed through `crud_dependency` at all, since it reads a different
    model entirely.

    `event_source_dependency`, if given, adds `GET <prefix>/events`: a
    Server-Sent Events stream of this resource's create/update/update_many/
    delete/delete_many/restore/restore_many activity (see
    craftainer_tool_crud.interfaces.base.EventSink/EventSource and
    docs/adrs/0015-mqtt-for-crud-events.md). Gated by the same `read_roles`
    dependency as the plain `GET` list route above.

    `stats_enabled=True` adds `GET <prefix>/stats` (count/numeric/categorical/
    time-series/lifecycle aggregates, see craftainer_tool_crud.controllers.crud_stats and
    craftainer_tool_crud.views.stats.ResourceStatsView) and `GET <prefix>/predict` (an
    ordinary-least-squares trend forecast over the same time-bucketed series,
    see craftainer_tool_crud.controllers.crud_stats.forecast and
    craftainer_tool_crud.views.stats.PredictionView)
    -- both gated by the same `read_roles` dependency as the plain `GET` list
    route.
    """
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
    rate_limit = _rate_limited(bulk_rate_limit)
    not_found = f"{resource_label} not found"

    @router.get("", dependencies=[read_roles])
    async def list_records(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
        skip: int = 0,
        limit: Annotated[int, Query(le=_MAX_LIMIT)] = 100,
    ) -> schema | list[schema]:  # type: ignore[valid-type]
        result = await resolve_list_or_get(
            crud, schema, request, id=id, skip=skip, limit=limit, not_found=not_found
        )
        headers = _revalidation_headers(result)
        if (unchanged := _not_modified(request, response, headers)) is not None:
            return unchanged
        response.headers.update(headers)
        return result  # type: ignore[no-any-return]

    @router.post("", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
    async def create_record(record: create_schema, crud: crud_dependency) -> schema:  # type: ignore[valid-type]
        return await crud.create(record)  # type: ignore[no-any-return]

    @router.get("/filters", dependencies=[read_roles])
    async def list_filters() -> list[FieldFilterInfo]:
        return describe_fields(schema)

    @router.patch("", dependencies=[write_roles])
    @rate_limit
    async def update_records(
        crud: crud_dependency,
        request: Request,
        response: Response,
        record: update_schema,  # type: ignore[valid-type]
        id: int | None = None,  # noqa: A002
    ) -> schema | BulkUpdateResult:  # type: ignore[valid-type]
        await _enforce_if_match(crud, request, id=id, not_found=not_found)
        result = await resolve_update(
            crud, schema, request, id=id, data=record, not_found=not_found
        )
        if not isinstance(result, BulkUpdateResult):
            response.headers["ETag"] = record_etag(result)
        return result  # type: ignore[no-any-return]

    @router.delete(
        "",
        dependencies=[delete_roles],
        response_model=None,
        responses={
            status.HTTP_200_OK: {"model": BulkDeleteResult},
            status.HTTP_204_NO_CONTENT: {"description": "Single record deleted"},
        },
    )
    @rate_limit
    async def delete_records(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
    ) -> Response:
        await _enforce_if_match(crud, request, id=id, not_found=not_found)
        result = await resolve_delete(crud, schema, request, id=id, not_found=not_found)
        if result is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        # Serialise straight from pydantic: skips FastAPI's jsonable_encoder + JSONResponse
        # round trip (same compact bytes); dependency headers are copied over by hand.
        return _with_dependency_headers(
            response, Response(content=result.model_dump_json(), media_type="application/json")
        )

    @router.post("/clone", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
    async def clone_record(id: int, crud: crud_dependency) -> schema:  # type: ignore[valid-type] # noqa: A002
        record = await crud.get(id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        clone_fields = {field: getattr(record, field) for field in create_schema.model_fields}
        clone_data = create_schema.model_validate(clone_fields)
        return await crud.create(clone_data)  # type: ignore[no-any-return]

    if draft_schema is not None:

        @router.post("/draft", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
        async def create_draft(record: draft_schema, crud: crud_dependency) -> schema:  # type: ignore[valid-type]
            return await crud.create(record)  # type: ignore[no-any-return]

        @router.post("/publish", dependencies=[write_roles])
        async def publish_record(id: int, crud: crud_dependency) -> schema:  # type: ignore[valid-type] # noqa: A002
            record = await crud.get(id)
            if record is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
            _validate_publish_ready(record, create_schema)
            # `crud.get` above is unscoped for a resource using owner=OwnerScope(...,
            # read_scoped=False) (see craftainer_tool_crud.interfaces.base.OwnerScope), but
            # `crud.update`
            # always applies the owner filter -- so a caller who can see someone else's
            # record here can still get None back from update instead of the record
            # itself. Without this check, that None would be returned as this route's
            # `-> schema` response, which FastAPI's response validation rejects as a 500
            # instead of the clean 404 a missing/inaccessible record should produce.
            published = await crud.update(id, _PublishFlip(is_draft=False))
            if published is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
            return published  # type: ignore[no-any-return]

    if archivable:

        @router.post("/restore", dependencies=[write_roles])
        @rate_limit
        async def restore_records(
            crud: crud_dependency,
            request: Request,
            id: int | None = None,  # noqa: A002
        ) -> schema | BulkUpdateResult:  # type: ignore[valid-type]
            return await resolve_restore(crud, schema, request, id=id, not_found=not_found)  # type: ignore[no-any-return]

    if revision_repository_dependency is not None and resource is not None:

        @router.get("/revisions", dependencies=[read_roles])
        async def list_revisions(
            repository: revision_repository_dependency,
            id: int,  # noqa: A002
        ) -> list[RevisionView]:
            records = await repository.list(
                filters=[
                    FilterClause("resource", FilterOp.EQ, resource),
                    FilterClause("record_id", FilterOp.EQ, id),
                ],
                sort=[SortClause("created_at", descending=True)],
                limit=_MAX_LIMIT,
            )
            return [RevisionView.model_validate(record) for record in records]

    if event_source_dependency is not None:

        @router.get("/events", dependencies=[read_roles])
        async def stream_events(
            source: event_source_dependency,
            request: Request,
            subscriber_id: str | None = None,
        ) -> StreamingResponse:
            resolved_subscriber_id = subscriber_id or request.headers.get("last-event-id")
            resolved_id, events = await source.subscribe(resolved_subscriber_id)
            return StreamingResponse(
                _sse_events(request, resolved_id, events), media_type="text/event-stream"
            )

    if stats_enabled:
        cache_resource = _require_cache_resource(stats_cache, resource)

        @router.get("/stats", response_model=ResourceStatsView, dependencies=[read_roles])
        async def get_stats(
            crud: crud_dependency, request: Request
        ) -> ResourceStatsView | Response:
            if stats_cache is None:
                return await _resolve_stats(crud, schema, request)
            key = crud_stats.cache_key(
                kind="stats",
                schema=schema,
                scope=crud_stats.cache_scope(crud),
                params=_stats_cache_params(request),
            )

            async def build() -> bytes:
                view = await _resolve_stats(crud, schema, request)
                return view.model_dump_json(by_alias=True).encode()

            body, state = await _cached_body(stats_cache, cache_resource, key, build)
            return Response(
                content=body, media_type="application/json", headers={_CACHE_HEADER: state}
            )

        @router.get("/predict", response_model=PredictionView, dependencies=[read_roles])
        async def get_prediction(
            crud: crud_dependency,
            field: str | None = None,
            periods: Annotated[
                int, Query(ge=crud_stats.MIN_PERIODS, le=crud_stats.MAX_PERIODS)
            ] = crud_stats.DEFAULT_PERIODS,
            bucket: str = "day",
        ) -> PredictionView | Response:
            if stats_cache is None:
                return await _resolve_predict(
                    crud, schema, field=field, periods=periods, bucket_raw=bucket
                )
            key = crud_stats.cache_key(
                kind="predict",
                schema=schema,
                scope=crud_stats.cache_scope(crud),
                params=_predict_cache_params(
                    schema, field=field, periods=periods, bucket_raw=bucket
                ),
            )

            async def build() -> bytes:
                view = await _resolve_predict(
                    crud, schema, field=field, periods=periods, bucket_raw=bucket
                )
                return view.model_dump_json(by_alias=True).encode()

            body, state = await _cached_body(stats_cache, cache_resource, key, build)
            return Response(
                content=body, media_type="application/json", headers={_CACHE_HEADER: state}
            )

    return router


async def _sse_events(
    request: Request, subscriber_id: str, events: AsyncIterator[dict[str, Any]]
) -> AsyncIterator[str]:
    """Render one EventSource subscription as an SSE byte stream.

    The first frame always carries `subscriber_id` (see EventSource.subscribe's own
    docstring) so a client that connected with none yet learns which one to pass back
    on reconnect. Every subsequent frame gets its own `id:` line, a per-connection
    sequence number -- unlike the MQTT-side persistent-session replay this rides on
    top of (see docs/adrs/0015-mqtt-for-crud-events.md), this sequence number is
    purely informational and isn't itself used to resume a gap. A `: keep-alive`
    comment fills any gap longer than `Settings.sse_keepalive_seconds` so intermediary
    proxies/load balancers don't time out an idle connection; the stream ends as soon
    as `request.is_disconnected()` reports the client is gone.

    Uses `asyncio.wait` (checking, not consuming, a still-pending task) rather than
    `asyncio.wait_for` around `iterator.__anext__()` -- `wait_for` cancels its inner
    awaitable on timeout, which would tear down `events`'s underlying async generator
    (e.g. InMemoryEventSink._events/MQTTEventSource._events, both suspended in an
    `await` at that point) on every single keep-alive, silently ending the stream one
    keep-alive after it started. The one pending `__anext__()` task survives across
    keep-alives and is only ever cancelled once the client actually disconnects.

    The `finally` below only cancels `pending`, never awaits it: on a real ASGI
    server, this generator's own cancellation (see the loop's own comment) leaves no
    room for a further `await` here to run to completion -- it gets cancelled again
    immediately. `events`'s own cleanup (MQTTEventSource._events, InMemoryEventSink
    needs none) accounts for this itself, via a detached task that outlives this
    generator's cancellation -- see its own docstring.
    """
    yield f"id: 0\ndata: {json.dumps({'subscriber_id': subscriber_id})}\n\n"
    sequence = 1
    iterator = events.__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        while True:
            # On uvicorn (confirmed by tracing a real disconnect: uvicorn's own
            # RequestResponseCycle.run_asgi() cancels this whole generator's task
            # directly, independent of anything below), the server itself notices a
            # closed connection and cancels this generator -- via the `finally` below
            # -- before this poll ever observes True; this is a defensive fallback for
            # an ASGI server that doesn't do that, and is exercised directly (with a
            # fake Request) by tests/unit/controllers/test_crud_router.py's
            # test_sse_events_ends_on_client_disconnect.
            if await request.is_disconnected():  # pragma: no cover
                return
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=get_config().sse_keepalive_seconds)
            if not done:
                yield ": keep-alive\n\n"
                continue
            pending = None
            try:
                event = done.pop().result()
            # Neither production EventSource's `_events` (InMemoryEventSink's `while
            # True: yield await queue.get()`, MQTTEventSource's `async for message in
            # client.messages`) ever returns on its own; this is defensive only, and
            # exercised directly (with a source that does end) by
            # tests/unit/controllers/test_crud_router.py's
            # test_sse_events_yields_subscriber_id_frame_then_each_event.
            except StopAsyncIteration:  # pragma: no cover
                return
            yield f"id: {sequence}\ndata: {json.dumps(event)}\n\n"
            sequence += 1
    finally:
        if pending is not None:
            pending.cancel()


def build_xml_router[SchemaT: BaseModel, CreateT: BaseModel, UpdateT: BaseModel](
    *,
    prefix: str,
    tags: Sequence[str],
    resource_label: str,
    item_tag: str,
    list_tag: str,
    schema: type[SchemaT],
    create_schema: type[CreateT],
    update_schema: type[UpdateT],
    crud_dependency: Any,
    read_roles: Any,
    write_roles: Any,
    delete_roles: Any,
    router_dependencies: Sequence[Any] = (),
    draft_schema: Any = None,  # type[BaseModel] | None -- see build_json_router
    archivable: bool = False,
    revision_repository_dependency: Any = None,  # Annotated[Repository[Revision], Depends(...)]
    resource: str | None = None,
    event_source_dependency: Any = None,  # Annotated[EventSource, Depends(...)]
    stats_enabled: bool = False,
    bulk_rate_limit: RateLimit | None = None,
    stats_cache: StatsCache | None = None,
) -> APIRouter:
    """Build the XML-flavored sibling of build_json_router's routes.

    Full parity with build_json_router: id/filter/bulk actions, plus (via the
    same opt-in params) restore/draft/publish/revisions/events/stats/predict.
    `GET <prefix>/stats`/`GET <prefix>/predict` are hand-assembled nested XML
    (see `_stats_to_xml`/`_prediction_to_xml`) rather than a single `to_xml`
    call -- craftainer_tool_crud.xml_codec.to_xml only supports a flat model (see its own module
    docstring), and stats/predictions are naturally nested (per-field
    aggregates, a distribution, a time series). `GET <prefix>/events` stays a
    JSON-payload SSE stream even here -- SSE's `data:` line is a transport
    envelope, not a resource representation (see craftainer_tool_crud.interfaces.base.
    EventSink/EventSource's own docstrings), so it's exempt from this router's
    otherwise-XML rendering.
    """
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
    rate_limit = _rate_limited(bulk_rate_limit)
    not_found = f"{resource_label} not found"
    xml_media_type = "application/xml"

    @router.get("", dependencies=[read_roles])
    async def list_records_xml(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
        skip: int = 0,
        limit: Annotated[int, Query(le=_MAX_LIMIT)] = 100,
    ) -> Response:
        result = await resolve_list_or_get(
            crud, schema, request, id=id, skip=skip, limit=limit, not_found=not_found
        )
        headers = _revalidation_headers(result)
        if (unchanged := _not_modified(request, response, headers)) is not None:
            return unchanged
        if isinstance(result, list):
            body = f"<{list_tag}>" + "".join(to_xml(r, item_tag) for r in result) + f"</{list_tag}>"
        else:
            body = to_xml(result, item_tag)
        return _with_dependency_headers(
            response, Response(content=body, media_type=xml_media_type, headers=headers)
        )

    @router.post("", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
    async def create_record_xml(
        crud: crud_dependency, request: Request, response: Response
    ) -> Response:
        record = _parse_xml_body(await request.body(), create_schema)
        created = await crud.create(record)
        return _with_dependency_headers(
            response,
            Response(
                content=to_xml(created, item_tag),
                media_type=xml_media_type,
                status_code=status.HTTP_201_CREATED,
            ),
        )

    @router.patch("", dependencies=[write_roles])
    @rate_limit
    async def update_records_xml(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
    ) -> Response:
        record = _parse_xml_body(await request.body(), update_schema)
        await _enforce_if_match(crud, request, id=id, not_found=not_found)
        result = await resolve_update(
            crud, schema, request, id=id, data=record, not_found=not_found
        )
        if isinstance(result, BulkUpdateResult):
            body = to_xml(result, "bulk-update-result")
            headers = {}
        else:
            body = to_xml(result, item_tag)
            headers = {"ETag": record_etag(result)}
        return _with_dependency_headers(
            response, Response(content=body, media_type=xml_media_type, headers=headers)
        )

    @router.delete("", dependencies=[delete_roles])
    @rate_limit
    async def delete_records_xml(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
    ) -> Response:
        await _enforce_if_match(crud, request, id=id, not_found=not_found)
        result = await resolve_delete(crud, schema, request, id=id, not_found=not_found)
        if result is None:
            built = Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            built = Response(
                content=to_xml(result, "bulk-delete-result"), media_type=xml_media_type
            )
        return _with_dependency_headers(response, built)

    @router.post("/clone", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
    async def clone_record_xml(
        id: int,  # noqa: A002
        crud: crud_dependency,
        response: Response,
    ) -> Response:
        record = await crud.get(id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        clone_fields = {field: getattr(record, field) for field in create_schema.model_fields}
        clone_data = create_schema.model_validate(clone_fields)
        created = await crud.create(clone_data)
        return _with_dependency_headers(
            response,
            Response(
                content=to_xml(created, item_tag),
                media_type=xml_media_type,
                status_code=status.HTTP_201_CREATED,
            ),
        )

    if draft_schema is not None:

        @router.post("/draft", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
        async def create_draft_xml(
            request: Request, crud: crud_dependency, response: Response
        ) -> Response:
            record = _parse_xml_body(await request.body(), draft_schema)
            created = await crud.create(record)
            return _with_dependency_headers(
                response,
                Response(
                    content=to_xml(created, item_tag),
                    media_type=xml_media_type,
                    status_code=status.HTTP_201_CREATED,
                ),
            )

        @router.post("/publish", dependencies=[write_roles])
        async def publish_record_xml(
            id: int,  # noqa: A002
            crud: crud_dependency,
            response: Response,
        ) -> Response:
            record = await crud.get(id)
            if record is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
            _validate_publish_ready(record, create_schema)
            published = await crud.update(id, _PublishFlip(is_draft=False))
            if published is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
            return _with_dependency_headers(
                response, Response(content=to_xml(published, item_tag), media_type=xml_media_type)
            )

    if archivable:

        @router.post("/restore", dependencies=[write_roles])
        @rate_limit
        async def restore_records_xml(
            crud: crud_dependency,
            request: Request,
            response: Response,
            id: int | None = None,  # noqa: A002
        ) -> Response:
            result = await resolve_restore(crud, schema, request, id=id, not_found=not_found)
            if isinstance(result, BulkUpdateResult):
                body = to_xml(result, "bulk-update-result")
            else:
                body = to_xml(result, item_tag)
            return _with_dependency_headers(
                response, Response(content=body, media_type=xml_media_type)
            )

    if revision_repository_dependency is not None and resource is not None:

        @router.get("/revisions", dependencies=[read_roles])
        async def list_revisions_xml(
            repository: revision_repository_dependency,
            response: Response,
            id: int,  # noqa: A002
        ) -> Response:
            records = await repository.list(
                filters=[
                    FilterClause("resource", FilterOp.EQ, resource),
                    FilterClause("record_id", FilterOp.EQ, id),
                ],
                sort=[SortClause("created_at", descending=True)],
                limit=_MAX_LIMIT,
            )
            revisions = [RevisionView.model_validate(record) for record in records]
            revisions_xml = "".join(to_xml(r, "revision") for r in revisions)
            body = f"<revisions>{revisions_xml}</revisions>"
            return _with_dependency_headers(
                response, Response(content=body, media_type=xml_media_type)
            )

    if event_source_dependency is not None:

        @router.get("/events", dependencies=[read_roles])
        async def stream_events_xml(
            source: event_source_dependency,
            request: Request,
            subscriber_id: str | None = None,
        ) -> StreamingResponse:
            resolved_subscriber_id = subscriber_id or request.headers.get("last-event-id")
            resolved_id, events = await source.subscribe(resolved_subscriber_id)
            return StreamingResponse(
                _sse_events(request, resolved_id, events), media_type="text/event-stream"
            )

    if stats_enabled:
        cache_resource = _require_cache_resource(stats_cache, resource)

        @router.get("/stats", dependencies=[read_roles])
        async def get_stats_xml(
            crud: crud_dependency, request: Request, response: Response
        ) -> Response:
            if stats_cache is None:
                view = await _resolve_stats(crud, schema, request)
                return _with_dependency_headers(
                    response, Response(content=_stats_to_xml(view), media_type=xml_media_type)
                )
            key = crud_stats.cache_key(
                kind="stats-xml",
                schema=schema,
                scope=crud_stats.cache_scope(crud),
                params=_stats_cache_params(request),
            )

            async def build() -> bytes:
                view = await _resolve_stats(crud, schema, request)
                return _stats_to_xml(view).encode()

            body, state = await _cached_body(stats_cache, cache_resource, key, build)
            return _with_dependency_headers(
                response,
                Response(content=body, media_type=xml_media_type, headers={_CACHE_HEADER: state}),
            )

        @router.get("/predict", dependencies=[read_roles])
        async def get_prediction_xml(
            crud: crud_dependency,
            response: Response,
            field: str | None = None,
            periods: Annotated[
                int, Query(ge=crud_stats.MIN_PERIODS, le=crud_stats.MAX_PERIODS)
            ] = crud_stats.DEFAULT_PERIODS,
            bucket: str = "day",
        ) -> Response:
            if stats_cache is None:
                view = await _resolve_predict(
                    crud, schema, field=field, periods=periods, bucket_raw=bucket
                )
                return _with_dependency_headers(
                    response,
                    Response(content=_prediction_to_xml(view), media_type=xml_media_type),
                )
            key = crud_stats.cache_key(
                kind="predict-xml",
                schema=schema,
                scope=crud_stats.cache_scope(crud),
                params=_predict_cache_params(
                    schema, field=field, periods=periods, bucket_raw=bucket
                ),
            )

            async def build() -> bytes:
                view = await _resolve_predict(
                    crud, schema, field=field, periods=periods, bucket_raw=bucket
                )
                return _prediction_to_xml(view).encode()

            body, state = await _cached_body(stats_cache, cache_resource, key, build)
            return _with_dependency_headers(
                response,
                Response(content=body, media_type=xml_media_type, headers={_CACHE_HEADER: state}),
            )

    return router


def build_web_router[CreateT: BaseModel](
    *,
    prefix: str,
    tags: Sequence[str],
    resource: str,
    api_base: str,
    fields: Sequence[str],
    create_schema: type[CreateT],
    crud_dependency: Any,
    read_roles: Any,
    write_roles: Any,
    router_dependencies: Sequence[Any] = (),
    draft_schema: Any = None,  # type[BaseModel] | None -- see build_json_router
    archivable: bool = False,
    revision_repository_dependency: Any = None,  # Annotated[Repository[Revision], Depends(...)]
    event_source_dependency: Any = None,  # Annotated[EventSource, Depends(...)]
    stats_enabled: bool = False,
) -> APIRouter:
    """Build the zero-JS-form + web-component-JS sibling router for one resource.

    `draft_schema`/`archivable`/`revision_repository_dependency`/
    `event_source_dependency`/`stats_enabled` mirror build_json_router's own
    same-named params -- this router adds no new FastAPI routes for them (the
    sibling JSON router already serves `/restore`/`/draft`/`/publish`/
    `/revisions`/`/events`/`/stats`/`/predict`, see build_json_router); they're
    only used here to decide which UI craftainer_tool_crud.web_components.render_crud_component_js
    generates (an Archive/Restore action, a Save-as-draft/Publish pair, a History
    panel, a live-events subscription, a Stats/Predict panel), calling those
    JSON routes directly, same as every other action already does.

    `list_fields` (which of `fields` are arrays, for comma-split parsing and
    ", "-joined display) is derived from `create_schema`'s own annotations via
    `is_list_annotation`, rather than passed separately -- one less value for a
    caller to keep in sync with its own view module.

    The rendered web component (`craftainer_tool_crud.web_components.render_crud_component_js`)
    talks directly to `api_base` -- the sibling build_json_router's own prefix --
    for every list/create/update/delete/filters-metadata call, so this router
    itself only ever serves `/form` and `/components.js`; filtering/sorting/bulk
    actions reach the UI automatically once the JSON router that `api_base`
    points at supports them, with no separate data routes needed here.

    A submitted form redirects back to `request.url.path` (this route's own
    URL), not `api_base` -- the two only coincided by construction while every
    format shared one prefix; since `build_resource_router` mounts JSON/XML/web
    under their own explicit sub-prefixes, `api_base` names a sibling path that
    is no longer this router's own.

    The generic `Request.form()` parsing this needs (fields aren't known until
    runtime) loses FastAPI's typed-`Form()` per-field OpenAPI documentation, so
    `openapi_extra` rebuilds an equivalent requestBody schema by hand -- every
    field as a required string, matching what the form actually submits (a list
    field's comma-separated raw value, not the parsed array).
    """
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
    list_fields = tuple(
        f for f in fields if is_list_annotation(create_schema.model_fields[f].annotation)
    )
    form_openapi_extra = {
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "properties": {field: {"type": "string"} for field in fields},
                        "required": list(fields),
                    }
                }
            },
        }
    }

    @router.get("/form", dependencies=[read_roles])
    async def form_page(request: Request, response: Response) -> Response:
        own_base = request.url.path.removesuffix("/form")
        return _with_dependency_headers(
            response,
            Response(
                content=render_crud_form(resource, fields, api_base, own_base),
                media_type="text/html",
            ),
        )

    @router.post(
        "/form",
        status_code=status.HTTP_303_SEE_OTHER,
        dependencies=[write_roles],
        openapi_extra=form_openapi_extra,
    )
    async def submit_form(
        request: Request, crud: crud_dependency, response: Response
    ) -> RedirectResponse:
        form = await request.form()
        data: dict[str, str | list[str]] = {}
        for field in fields:
            raw = str(form.get(field, ""))
            if field in list_fields:
                data[field] = [v.strip() for v in raw.split(",") if v.strip()]
            # A blank optional non-list field (e.g. Hero's power_level) submits as ""
            # like any other unfilled <input> -- omitted here rather than passed
            # through so create_schema falls back to its own field default (None)
            # instead of failing "" as an invalid int/etc; a blank *required* field
            # still 422s, just via create_schema's own "missing" error instead.
            elif raw:
                data[field] = raw
        try:
            validated = create_schema.model_validate(data)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        await crud.create(validated)
        return _with_dependency_headers(
            response, RedirectResponse(request.url.path, status_code=status.HTTP_303_SEE_OTHER)
        )

    @router.get("/components.js")
    async def components_js(response: Response) -> Response:
        return _with_dependency_headers(
            response,
            Response(
                content=render_crud_component_js(
                    resource,
                    api_base,
                    fields,
                    list_fields=list_fields,
                    archivable=archivable,
                    draftable=draft_schema is not None,
                    has_revisions=revision_repository_dependency is not None,
                    has_events=event_source_dependency is not None,
                    stats_enabled=stats_enabled,
                ),
                media_type="application/javascript",
            ),
        )

    return router


def build_resource_router[SchemaT: BaseModel, CreateT: BaseModel, UpdateT: BaseModel](
    *,
    prefix: str,
    api_prefix: str | None = None,
    tags: Sequence[str],
    resource_label: str,
    resource: str,
    item_tag: str,
    list_tag: str,
    fields: Sequence[str],
    schema: type[SchemaT],
    create_schema: type[CreateT],
    update_schema: type[UpdateT],
    crud_dependency: Any,  # Annotated[CRUDLike[SchemaT], Depends(...)]
    read_roles: Any,  # a Depends(...) object, e.g. heroes.ReadRoles
    write_roles: Any,
    delete_roles: Any,
    router_dependencies: Sequence[Any] = (),
    draft_schema: Any = None,  # type[BaseModel] | None -- see build_json_router
    archivable: bool = False,
    revision_repository_dependency: Any = None,  # Annotated[Repository[Revision], Depends(...)]
    event_source_dependency: Any = None,  # Annotated[EventSource, Depends(...)]
    stats_enabled: bool = False,
    bulk_rate_limit: RateLimit | None = None,
    stats_cache: StatsCache | None = None,
) -> APIRouter:
    """Compose build_json_router/build_xml_router/build_web_router into one resource-version router.

    `prefix` is this router's own prefix, resource-relative to wherever a caller
    later mounts the returned router (e.g. `/heroes/v2`, see
    docs/adrs/0009-...md) -- each format is mounted under it as its own explicit,
    non-empty `/json`/`/xml`/`/web` sub-prefix.

    `api_prefix` is the full, browser-reachable path to this router's `/json`
    sub-router (e.g. `/crud/v1/heroes/v2`) -- used only to compute
    `build_web_router`'s `api_base`, which gets baked into rendered HTML/JS at
    build time and so can't be derived from a later `include_router` call's own
    prefix the way FastAPI's own routing can. Defaults to `prefix`, for a router
    mounted with no further outer prefix.

    `router_dependencies` (e.g. app.http_headers.sunset(...) for a deprecated
    version) is applied once, on this router's own constructor -- FastAPI merges a
    router's own `dependencies` into every route of a sub-router later
    `include_router`'d into it, so this single declaration reaches JSON/XML/web
    alike, rather than each per-format factory call repeating it.

    `draft_schema`/`archivable`/`revision_repository_dependency`/
    `event_source_dependency`/`stats_enabled` are forwarded to all three
    factories identically -- draft/publish/restore/revisions/events/stats/
    predict are first-class across JSON/XML/web, not JSON-only (see
    build_json_router's/build_xml_router's/build_web_router's own docstrings).

    `bulk_rate_limit` is an optional route decorator (e.g. `limiter.limit("20/minute")`)
    applied to the JSON and XML bulk-action routes; None leaves them unlimited.

    `stats_cache` is forwarded to the JSON and XML factories; see build_json_router.
    """
    full_prefix = prefix if api_prefix is None else api_prefix
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
    router.include_router(
        build_json_router(
            prefix="",
            tags=tags,
            resource_label=resource_label,
            schema=schema,
            create_schema=create_schema,
            update_schema=update_schema,
            crud_dependency=crud_dependency,
            read_roles=read_roles,
            write_roles=write_roles,
            delete_roles=delete_roles,
            draft_schema=draft_schema,
            archivable=archivable,
            revision_repository_dependency=revision_repository_dependency,
            resource=resource,
            event_source_dependency=event_source_dependency,
            stats_enabled=stats_enabled,
            bulk_rate_limit=bulk_rate_limit,
            stats_cache=stats_cache,
        ),
        prefix="/json",
    )
    router.include_router(
        build_xml_router(
            prefix="",
            tags=tags,
            resource_label=resource_label,
            item_tag=item_tag,
            list_tag=list_tag,
            schema=schema,
            create_schema=create_schema,
            update_schema=update_schema,
            crud_dependency=crud_dependency,
            read_roles=read_roles,
            write_roles=write_roles,
            delete_roles=delete_roles,
            draft_schema=draft_schema,
            archivable=archivable,
            revision_repository_dependency=revision_repository_dependency,
            resource=resource,
            event_source_dependency=event_source_dependency,
            stats_enabled=stats_enabled,
            bulk_rate_limit=bulk_rate_limit,
            stats_cache=stats_cache,
        ),
        prefix="/xml",
    )
    router.include_router(
        build_web_router(
            prefix="",
            tags=tags,
            resource=resource,
            api_base=f"{full_prefix}/json",
            fields=fields,
            create_schema=create_schema,
            crud_dependency=crud_dependency,
            read_roles=read_roles,
            write_roles=write_roles,
            draft_schema=draft_schema,
            archivable=archivable,
            revision_repository_dependency=revision_repository_dependency,
            event_source_dependency=event_source_dependency,
            stats_enabled=stats_enabled,
        ),
        prefix="/web",
    )
    return router
