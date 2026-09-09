"""Generic FastAPI router factories for a resource's CRUD endpoints.

Parameterized by a resource's Pydantic views and an already-built CRUD
dependency (crud.interfaces.base.CRUDLike) -- covers both a current version
(CRUDInterface) and a deprecated one (CompatCRUD) identically, since
both satisfy CRUDLike structurally. Each factory builds its route
functions internally as closures over its arguments: the generated
functions' `crud`/`record` parameters are annotated with a TypeVar-bound
runtime type, which mypy cannot verify statically -- each such line
carries a narrow `# type: ignore[valid-type]`. This factory's own
signature (every parameter, `-> APIRouter`) stays fully strict-typed,
so a caller gets real type-checking on the call itself.

Record addressing is a query parameter (`?id=`), not a path segment, on every
factory here -- see crud.controllers.crud_actions for the shared "id present ->
single record; otherwise -> filtered list, or a bulk update/delete over the
given filters" logic each factory's routes wrap in their own response format.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Annotated, Any

from defusedxml.common import DefusedXmlException
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.rate_limit import limiter
from app.web_components import render_crud_component_js, render_crud_form
from app.xml_codec import from_xml, is_list_annotation, to_xml
from crud.controllers import crud_stats
from crud.controllers.crud_actions import (
    resolve_delete,
    resolve_list_or_get,
    resolve_restore,
    resolve_update,
)
from crud.controllers.crud_query import (
    FieldFilterInfo,
    describe_fields,
    parse_include_archived,
    parse_include_unpublished,
)
from crud.repositories.filtering import FilterClause, FilterOp, SortClause
from crud.repositories.stats import TimeBucket
from crud.views.bulk import BulkDeleteResult, BulkUpdateResult
from crud.views.revision import RevisionView
from crud.views.stats import (
    CategoricalValueCountView,
    LifecycleStatsView,
    NumericFieldStatView,
    PredictionView,
    ResourceStatsView,
    SeriesPointView,
    TimeBucketCountView,
)

settings = get_settings()

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
    crud.views.stats.PredictionView -- rendered as their own flat model via
    app.xml_codec.to_xml the same way a numeric-field/categorical-value/
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
    see crud.controllers.crud_stats.forecast's own docstring for the latter.
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


def _stats_to_xml(view: ResourceStatsView) -> str:
    """Hand-assemble `ResourceStatsView` as nested XML -- see build_xml_router's `/stats` route.

    `view.lifecycle is None`'s branch below is `# pragma: no cover`: it's populated
    whenever the underlying model carries *any* record-lifecycle mixin (see
    crud.repositories.memory/sqlalchemy's own `stats`), and Hero -- the only
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
) -> APIRouter:
    """Build the standard list/create/get/update/delete JSON router for one resource.

    `id`, when present as a query parameter, addresses a single record on the
    GET/PATCH/DELETE routes (404 if missing) -- otherwise GET lists (optionally
    filtered/sorted per crud.controllers.crud_query) and PATCH/DELETE act in bulk
    over whatever filters are given, rejecting an empty filter set with no `id`
    (400) so an empty query string can never target every record by accident.

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
    table (see crud.models.revision) for that record's history, newest first --
    not routed through `crud_dependency` at all, since it reads a different
    model entirely.

    `event_source_dependency`, if given, adds `GET <prefix>/events`: a
    Server-Sent Events stream of this resource's create/update/update_many/
    delete/delete_many/restore/restore_many activity (see
    crud.interfaces.base.EventSink/EventSource and
    docs/adrs/0015-mqtt-for-crud-events.md). Gated by the same `read_roles`
    dependency as the plain `GET` list route above.

    `stats_enabled=True` adds `GET <prefix>/stats` (count/numeric/categorical/
    time-series/lifecycle aggregates, see crud.controllers.crud_stats and
    crud.views.stats.ResourceStatsView) and `GET <prefix>/predict` (an
    ordinary-least-squares trend forecast over the same time-bucketed series,
    see crud.controllers.crud_stats.forecast and crud.views.stats.PredictionView)
    -- both gated by the same `read_roles` dependency as the plain `GET` list
    route.
    """
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
    not_found = f"{resource_label} not found"

    @router.get("", dependencies=[read_roles])
    async def list_records(
        crud: crud_dependency,
        request: Request,
        id: int | None = None,  # noqa: A002
        skip: int = 0,
        limit: Annotated[int, Query(le=_MAX_LIMIT)] = 100,
    ) -> schema | list[schema]:  # type: ignore[valid-type]
        return await resolve_list_or_get(  # type: ignore[no-any-return]
            crud, schema, request, id=id, skip=skip, limit=limit, not_found=not_found
        )

    @router.post("", status_code=status.HTTP_201_CREATED, dependencies=[write_roles])
    async def create_record(record: create_schema, crud: crud_dependency) -> schema:  # type: ignore[valid-type]
        return await crud.create(record)  # type: ignore[no-any-return]

    @router.get("/filters", dependencies=[read_roles])
    async def list_filters() -> list[FieldFilterInfo]:
        return describe_fields(schema)

    @router.patch("", dependencies=[write_roles])
    @limiter.limit(settings.rate_limit_bulk_action)
    async def update_records(
        crud: crud_dependency,
        request: Request,
        record: update_schema,  # type: ignore[valid-type]
        id: int | None = None,  # noqa: A002
    ) -> schema | BulkUpdateResult:  # type: ignore[valid-type]
        return await resolve_update(crud, schema, request, id=id, data=record, not_found=not_found)  # type: ignore[no-any-return]

    @router.delete("", dependencies=[delete_roles], response_model=None)
    @limiter.limit(settings.rate_limit_bulk_action)
    async def delete_records(
        crud: crud_dependency,
        request: Request,
        id: int | None = None,  # noqa: A002
    ) -> BulkDeleteResult | Response:
        result = await resolve_delete(crud, schema, request, id=id, not_found=not_found)
        return result if result is not None else Response(status_code=status.HTTP_204_NO_CONTENT)

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
            # read_scoped=False) (see crud.interfaces.base.OwnerScope), but `crud.update`
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
        @limiter.limit(settings.rate_limit_bulk_action)
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

        @router.get("/stats", dependencies=[read_roles])
        async def get_stats(crud: crud_dependency, request: Request) -> ResourceStatsView:
            return await _resolve_stats(crud, schema, request)

        @router.get("/predict", dependencies=[read_roles])
        async def get_prediction(
            crud: crud_dependency,
            field: str | None = None,
            periods: Annotated[
                int, Query(ge=crud_stats.MIN_PERIODS, le=crud_stats.MAX_PERIODS)
            ] = crud_stats.DEFAULT_PERIODS,
            bucket: str = "day",
        ) -> PredictionView:
            return await _resolve_predict(
                crud, schema, field=field, periods=periods, bucket_raw=bucket
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
            done, _ = await asyncio.wait({pending}, timeout=settings.sse_keepalive_seconds)
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
) -> APIRouter:
    """Build the XML-flavored sibling of build_json_router's routes.

    Full parity with build_json_router: id/filter/bulk actions, plus (via the
    same opt-in params) restore/draft/publish/revisions/events/stats/predict.
    `GET <prefix>/stats`/`GET <prefix>/predict` are hand-assembled nested XML
    (see `_stats_to_xml`/`_prediction_to_xml`) rather than a single `to_xml`
    call -- app.xml_codec.to_xml only supports a flat model (see its own module
    docstring), and stats/predictions are naturally nested (per-field
    aggregates, a distribution, a time series). `GET <prefix>/events` stays a
    JSON-payload SSE stream even here -- SSE's `data:` line is a transport
    envelope, not a resource representation (see crud.interfaces.base.
    EventSink/EventSource's own docstrings), so it's exempt from this router's
    otherwise-XML rendering.
    """
    router = APIRouter(prefix=prefix, tags=list(tags), dependencies=list(router_dependencies))
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
        if isinstance(result, list):
            body = f"<{list_tag}>" + "".join(to_xml(r, item_tag) for r in result) + f"</{list_tag}>"
        else:
            body = to_xml(result, item_tag)
        return _with_dependency_headers(response, Response(content=body, media_type=xml_media_type))

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
    @limiter.limit(settings.rate_limit_bulk_action)
    async def update_records_xml(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
    ) -> Response:
        record = _parse_xml_body(await request.body(), update_schema)
        result = await resolve_update(
            crud, schema, request, id=id, data=record, not_found=not_found
        )
        if isinstance(result, BulkUpdateResult):
            body = to_xml(result, "bulk-update-result")
        else:
            body = to_xml(result, item_tag)
        return _with_dependency_headers(response, Response(content=body, media_type=xml_media_type))

    @router.delete("", dependencies=[delete_roles])
    @limiter.limit(settings.rate_limit_bulk_action)
    async def delete_records_xml(
        crud: crud_dependency,
        request: Request,
        response: Response,
        id: int | None = None,  # noqa: A002
    ) -> Response:
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
        @limiter.limit(settings.rate_limit_bulk_action)
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

        @router.get("/stats", dependencies=[read_roles])
        async def get_stats_xml(
            crud: crud_dependency, request: Request, response: Response
        ) -> Response:
            view = await _resolve_stats(crud, schema, request)
            return _with_dependency_headers(
                response, Response(content=_stats_to_xml(view), media_type=xml_media_type)
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
            view = await _resolve_predict(
                crud, schema, field=field, periods=periods, bucket_raw=bucket
            )
            return _with_dependency_headers(
                response, Response(content=_prediction_to_xml(view), media_type=xml_media_type)
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
    only used here to decide which UI app.web_components.render_crud_component_js
    generates (an Archive/Restore action, a Save-as-draft/Publish pair, a History
    panel, a live-events subscription, a Stats/Predict panel), calling those
    JSON routes directly, same as every other action already does.

    `list_fields` (which of `fields` are arrays, for comma-split parsing and
    ", "-joined display) is derived from `create_schema`'s own annotations via
    `is_list_annotation`, rather than passed separately -- one less value for a
    caller to keep in sync with its own view module.

    The rendered web component (`app.web_components.render_crud_component_js`)
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
