"""Shared id/filter/bulk decision logic for the CRUD router factories.

Route bodies factored out of crud.controllers.crud_router so build_json_router and
build_xml_router share one implementation of "id present -> single record;
otherwise -> filtered list, or a bulk update/delete over whatever filters are
given" -- each router factory wraps the same calls in its own response format
(a plain value for JSON, an XML-rendered Response for XML). build_json_router's
and build_xml_router's own public signatures are unchanged by this split.
"""

import logging
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from app.config import get_settings
from crud.controllers.crud_query import (
    parse_filters,
    parse_include_archived,
    parse_include_unpublished,
    parse_sort,
)
from crud.interfaces.base import CRUDLike
from crud.repositories.base import RecordLockedError
from crud.repositories.filtering import FilterClause
from crud.views.bulk import BulkDeleteResult, BulkUpdateResult

settings = get_settings()
logger = logging.getLogger(__name__)

_NO_TARGET_ERROR = [
    {
        "loc": ("query",),
        "msg": "at least one filter or id is required for a bulk action",
        "type": "value_error",
    }
]


def _actor(request: Request) -> str:
    """Return the authenticated caller's subject, for the audit log below.

    Reads the claims app.oidc.require_roles stashes on request.state (a route with
    no role requirement leaves this unset), rather than requiring every route to
    redeclare that dependency as a captured parameter just to pass it through here.
    """
    claims: dict[str, Any] | None = getattr(request.state, "claims", None)
    return "unknown" if not claims else str(claims.get("sub", "unknown"))


async def _check_bulk_action_size(
    crud: CRUDLike[Any], filters: list[FilterClause], *, include_archived: bool = False
) -> None:
    """Refuse a bulk update/delete whose filters match more than the configured cap.

    A technically-non-empty but always-true filter (e.g. id__gte=0) would otherwise
    still match every row -- counting first (cheap relative to the mutation itself)
    catches that before any row is touched. `include_archived` is passed through to
    `count()` so a bulk *restore* (which only ever targets archived rows) is sized
    correctly -- see resolve_restore below.

    The raise below is `# pragma: no cover` for tests/e2e specifically: triggering it
    live would mean creating over bulk_action_max_matched (1000 by default) records
    against the shared e2e stack, too slow/heavy for what tests/unit/controllers/
    test_crud_router.py already covers directly (with the cap monkeypatched low).
    """
    matched = await crud.count(filters=filters, include_archived=include_archived)
    if matched > settings.bulk_action_max_matched:  # pragma: no cover -- see docstring
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Bulk action would affect {matched} records, over the "
            f"{settings.bulk_action_max_matched}-record limit -- narrow the filters",
        )


async def resolve_list_or_get(
    crud: CRUDLike[Any],
    schema: type[BaseModel],
    request: Request,
    *,
    id: int | None,  # noqa: A002 -- the wire name this query param uses, see controllers/README.md
    skip: int,
    limit: int,
    not_found: str,
) -> Any:  # BaseModel | list[BaseModel], see module docstring
    """Return one record by id, or a filtered/sorted list of matching records.

    `?include_archived=true`/`?include_unpublished=true` (see crud.controllers.
    crud_query) are passed through unconditionally -- a CRUDInterface backed by a
    model with no Archivable/Schedulable mixin ignores them (see
    crud.repositories.sqlalchemy/crud.repositories.memory).
    """
    include_archived = parse_include_archived(request.query_params)
    include_unpublished = parse_include_unpublished(request.query_params)
    if id is not None:
        record = await crud.get(
            id, include_archived=include_archived, include_unpublished=include_unpublished
        )
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        return record
    filters = parse_filters(schema, request.query_params)
    sort = parse_sort(schema, request.query_params)
    return await crud.list(
        skip=skip,
        limit=limit,
        filters=filters,
        sort=sort,
        include_archived=include_archived,
        include_unpublished=include_unpublished,
    )


async def resolve_update(
    crud: CRUDLike[Any],
    schema: type[BaseModel],
    request: Request,
    *,
    id: int | None,  # noqa: A002
    data: BaseModel,
    not_found: str,
) -> Any:  # BaseModel | BulkUpdateResult, see module docstring
    """Update one record by id, or bulk-update every record matching the query filters."""
    if id is not None:
        try:
            updated = await crud.update(id, data)
        except RecordLockedError as exc:
            raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
        if updated is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        logger.info("Update: actor=%s path=%s id=%r", _actor(request), request.url.path, id)
        return updated
    filters = parse_filters(schema, request.query_params)
    if not filters:
        raise RequestValidationError(_NO_TARGET_ERROR)
    await _check_bulk_action_size(crud, filters)
    try:
        updated_records = await crud.update_many(filters=filters, data=data)
    except RecordLockedError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    ids = [record.id for record in updated_records]
    logger.info(
        "Bulk update: actor=%s path=%s filters=%r ids=%r",
        _actor(request),
        request.url.path,
        filters,
        ids,
    )
    return BulkUpdateResult(matched=len(updated_records), ids=ids)


async def resolve_delete(
    crud: CRUDLike[Any],
    schema: type[BaseModel],
    request: Request,
    *,
    id: int | None,  # noqa: A002
    not_found: str,
) -> BulkDeleteResult | None:
    """Delete one record by id, or bulk-delete every record matching the query filters."""
    if id is not None:
        try:
            deleted = await crud.delete(id)
        except RecordLockedError as exc:
            raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        logger.info("Delete: actor=%s path=%s id=%r", _actor(request), request.url.path, id)
        return None
    filters = parse_filters(schema, request.query_params)
    if not filters:
        raise RequestValidationError(_NO_TARGET_ERROR)
    await _check_bulk_action_size(crud, filters)
    try:
        deleted_records = await crud.delete_many(filters=filters)
    except RecordLockedError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    ids = [record.id for record in deleted_records]
    logger.info(
        "Bulk delete: actor=%s path=%s filters=%r ids=%r",
        _actor(request),
        request.url.path,
        filters,
        ids,
    )
    return BulkDeleteResult(
        matched=len(deleted_records),
        ids=ids,
    )


async def resolve_restore(
    crud: CRUDLike[Any],
    schema: type[BaseModel],
    request: Request,
    *,
    id: int | None,  # noqa: A002
    not_found: str,
) -> Any:  # BaseModel | BulkUpdateResult, see module docstring
    """Restore one archived record by id, or bulk-restore every record matching the filters."""
    if id is not None:
        restored = await crud.restore(id)  # type: ignore[attr-defined]
        if restored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        logger.info("Restore: actor=%s path=%s id=%r", _actor(request), request.url.path, id)
        return restored
    filters = parse_filters(schema, request.query_params)
    if not filters:
        raise RequestValidationError(_NO_TARGET_ERROR)
    await _check_bulk_action_size(crud, filters, include_archived=True)
    restored_records = await crud.restore_many(filters=filters)  # type: ignore[attr-defined]
    ids = [record.id for record in restored_records]
    logger.info(
        "Bulk restore: actor=%s path=%s filters=%r ids=%r",
        _actor(request),
        request.url.path,
        filters,
        ids,
    )
    return BulkUpdateResult(matched=len(restored_records), ids=ids)
