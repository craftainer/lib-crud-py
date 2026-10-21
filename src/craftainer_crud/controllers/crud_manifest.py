"""Routes that publish the CRUD manifest and the `api-catalog` that points at it.

`crud_manifest_router` serves `GET <prefix>/manifest`: the resources registered in a
`ManifestRegistry`, filtered to what the caller may read. Visibility is decided by
running each resource's own `read_roles` dependency, the one its list route runs, so the
manifest cannot disagree with access: a caller refused there (401 or 403) simply does not
see the resource. A resource registered `public_read` is listed for everyone. The body
carries an `ETag` over the caller-filtered document, so two callers who see different
resources get different tags, and a matching `If-None-Match` gets a bodiless `304`.

`api_catalog_router` serves `GET /.well-known/api-catalog` (RFC 9727, an RFC 9264 linkset):
mount it on the app root. It needs no authentication.

The shape is specified in docs/manifest.md and docs/adrs/0002-crud-manifest-v1.md.
"""

from contextlib import AsyncExitStack
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.dependencies.utils import get_dependant, solve_dependencies
from fastapi.responses import JSONResponse

from craftainer_crud.controllers.crud_router import (
    _REVALIDATE_CACHE_CONTROL,
    _digest,
    _not_modified,
)
from craftainer_crud.manifest import MANIFEST_PROFILE, ManifestRegistry, build_manifest
from craftainer_crud.manifest.registry import RegisteredResource

_LINKSET_MEDIA_TYPE = "application/linkset+json"
_OPENAPI_MEDIA_TYPE = "application/vnd.oai.openapi+json"


async def _may_read(
    request: Request,
    resource: RegisteredResource,
    stack: AsyncExitStack,
    dependency_cache: dict[Any, Any],
) -> bool:
    """Run the resource's own read-role dependency for this request; False if it refuses."""
    if resource.public_read:
        return True

    def probe(gate: Any = resource.read_gate) -> None:  # noqa: ANN401
        """Stand-in endpoint: solving its parameters runs the read-role dependency."""

    dependant = get_dependant(path=request.url.path, call=probe)
    try:
        solved = await solve_dependencies(
            request=request,
            dependant=dependant,
            dependency_overrides_provider=request.app,
            dependency_cache=dependency_cache,
            async_exit_stack=stack,
            embed_body_fields=False,
        )
    except HTTPException as exc:
        # Only a refusal hides the resource; an IdP outage (503) is the caller's to see.
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            return False
        raise
    return not solved.errors


def crud_manifest_router(
    registry: ManifestRegistry,
    *,
    service_name: str,
    service_version: str,
    stats_ttl: int | None = None,
    oidc_discovery_url: str | None = None,
    prefix: str = "/api/_crud",
) -> APIRouter:
    """Build the router serving `GET <prefix>/manifest` for the resources in `registry`.

    `stats_ttl` is the TTL in seconds of the host's cached `/stats` and `/predict` bodies,
    which the library cannot see; `None` reports "unknown or no cache". Resources are read
    from `registry` on every request, so routers registered after this call still appear.
    """
    router = APIRouter(prefix=prefix, tags=["crud-manifest"])

    @router.get("/manifest", response_model=None, responses={304: {"description": "Not modified"}})
    async def get_manifest(request: Request, response: Response) -> Response:
        dependency_cache: dict[Any, Any] = {}
        visible: list[RegisteredResource] = []
        async with AsyncExitStack() as stack:
            for resource in registry.resources():
                if await _may_read(request, resource, stack, dependency_cache):
                    visible.append(resource)
        manifest = build_manifest(
            visible,
            service_name=service_name,
            service_version=service_version,
            oidc_discovery_url=oidc_discovery_url,
            stats_ttl=stats_ttl,
        )
        body = manifest.model_dump_json().encode()
        headers = {
            "ETag": f'W/"{_digest(body.decode())}"',
            "Cache-Control": _REVALIDATE_CACHE_CONTROL,
        }
        not_modified = _not_modified(request, response, headers)
        if not_modified is not None:
            return not_modified
        return Response(content=body, media_type="application/json", headers=headers)

    return router


def api_catalog_router(
    *,
    manifest_url: str,
    openapi_url: str = "/openapi.json",
    anchor: str | None = None,
) -> APIRouter:
    """Build the router serving `GET /.well-known/api-catalog`; mount it at the app root.

    `manifest_url` is the manifest's URL as a client reaches it (for example
    `/api/_crud/manifest`); `openapi_url` is the OpenAPI document's. `anchor` is the API's
    base URL; by default it is the base URL the request arrived on.
    """
    router = APIRouter()

    @router.get("/.well-known/api-catalog", include_in_schema=False, response_model=None)
    async def get_api_catalog(request: Request) -> JSONResponse:
        linkset = {
            "linkset": [
                {
                    "anchor": anchor if anchor is not None else str(request.base_url).rstrip("/"),
                    "service-desc": [{"href": openapi_url, "type": _OPENAPI_MEDIA_TYPE}],
                    "describedby": [
                        {
                            "href": manifest_url,
                            "type": "application/json",
                            "profile": MANIFEST_PROFILE,
                        }
                    ],
                }
            ]
        }
        return JSONResponse(linkset, media_type=_LINKSET_MEDIA_TYPE)

    return router
