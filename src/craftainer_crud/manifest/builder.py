"""Build the manifest document from registered resources and the code the routers run.

Nothing here restates a rule the routers enforce: fields, operators and enum choices come
from `crud_query.field_specs`, the limits from `crud_query`'s constants and
`config.CrudConfig`, and the optional routes from what the factory registered.
"""

from collections.abc import Iterable
from datetime import date, datetime

from craftainer_crud.config import get_config
from craftainer_crud.controllers.crud_query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FieldKind,
    FieldSpec,
    field_specs,
)
from craftainer_crud.manifest.models import (
    MANIFEST_VERSION,
    Auth,
    Bulk,
    Cache,
    Capabilities,
    EventAction,
    Events,
    FieldInfo,
    Manifest,
    Pagination,
    ResourceManifest,
    SchemaSet,
    ServiceInfo,
    ValueType,
)
from craftainer_crud.manifest.registry import RegisteredResource

_VALUE_TYPE_BY_KIND: dict[str, ValueType] = {
    FieldKind.STRING: "string",
    FieldKind.BOOLEAN: "boolean",
    FieldKind.ENUM: "enum",
}
_NUMERIC: frozenset[ValueType] = frozenset({"integer", "number"})
_CATEGORICAL: frozenset[ValueType] = frozenset({"boolean", "enum"})


def _value_type(spec: FieldSpec) -> ValueType:
    """Refine the server's `kind` (dates and datetimes are both "number") into `value_type`."""
    if spec.kind != FieldKind.NUMBER:
        return _VALUE_TYPE_BY_KIND[spec.kind]
    python_type = spec.python_type
    if issubclass(python_type, datetime):
        return "datetime"
    if issubclass(python_type, date):
        return "date"
    return "integer" if issubclass(python_type, int) else "number"


def _fields(resource: RegisteredResource) -> list[FieldInfo]:
    create = resource.create_schema.model_fields
    update = resource.update_schema.model_fields
    result: list[FieldInfo] = []
    for name, spec in field_specs(resource.schema).items():
        value_type = _value_type(spec)
        result.append(
            FieldInfo(
                name=name,
                value_type=value_type,
                filter_ops=list(spec.ops),
                sortable=True,  # parse_sort accepts exactly field_specs' names
                choices=list(spec.choices) if spec.choices is not None else None,
                numeric=value_type in _NUMERIC,
                categorical=value_type in _CATEGORICAL,
                read_only=name not in create and name not in update,
                required=name in create and create[name].is_required(),
            )
        )
    return result


def _paths(resource: RegisteredResource) -> dict[str, str]:
    base = resource.base_path
    paths = {"filters": f"{base}/filters"}
    if resource.stats:
        paths["stats"] = f"{base}/stats"
        paths["predict"] = f"{base}/predict"
    if resource.revisions:
        paths["revisions"] = f"{base}/revisions"
    paths["clone"] = f"{base}/clone"
    if resource.draft_schema is not None:
        paths["draft"] = f"{base}/draft"
        paths["publish"] = f"{base}/publish"
    if resource.archivable:
        paths["restore"] = f"{base}/restore"
    if resource.events_resource is not None:
        paths["events"] = f"{base}/events"
    return paths


def _events(resource: RegisteredResource, paths: dict[str, str]) -> Events | None:
    if resource.events_resource is None:
        return None
    config = get_config()
    actions: list[EventAction] = ["create", "update", "delete"]
    if resource.archivable:
        actions.append("restore")
    return Events(
        transport="sse",
        path=paths["events"],
        resource=resource.events_resource,
        resume_param="subscriber_id",
        replay="none" if config.use_memory else "persistent",
        keepalive_seconds=config.sse_keepalive_seconds,
        actions=actions,
    )


def _resource_manifest(
    resource: RegisteredResource, *, oidc_discovery_url: str | None, stats_ttl: int | None
) -> ResourceManifest:
    paths = _paths(resource)
    return ResourceManifest(
        name=resource.name,
        title=resource.title,
        api_version=resource.api_version,
        base_path=resource.base_path,
        formats=resource.formats,
        id_field="id",
        schemas=SchemaSet(
            read=resource.schema.model_json_schema(mode="serialization"),
            create=resource.create_schema.model_json_schema(),
            update=resource.update_schema.model_json_schema(),
            draft=None
            if resource.draft_schema is None
            else resource.draft_schema.model_json_schema(),
        ),
        fields=_fields(resource),
        pagination=Pagination(
            style="offset",
            skip_param="skip",
            limit_param="limit",
            sort_param="sort",
            default_limit=DEFAULT_LIMIT,
            max_limit=MAX_LIMIT,
            total=False,
        ),
        capabilities=Capabilities(
            bulk_update=True,
            bulk_delete=True,
            clone=True,
            draft_publish=resource.draft_schema is not None,
            archive_restore=resource.archivable,
            revisions=resource.revisions,
            events=_events(resource, paths),
            stats=resource.stats,
            predict=resource.stats,
            conditional_requests=True,
            owner_scoped=resource.owner_scoped,
            schedulable=resource.schedulable,
        ),
        paths=paths,
        bulk=Bulk(max_matched=get_config().bulk_action_max_matched),
        auth=Auth(
            scheme="bearer",
            oidc_discovery_url=oidc_discovery_url,
            public_read=resource.public_read,
            read_roles=[] if resource.public_read else list(resource.read_roles),
            write_roles=list(resource.write_roles),
            delete_roles=list(resource.delete_roles),
        ),
        cache=Cache(
            etag=True,
            max_age=0,
            stats_ttl=stats_ttl if resource.stats else None,
            stale_while_revalidate=0,
            list_etag="weak",
            record_etag="strong",
            revalidate_only=True,
        ),
    )


def build_manifest(
    resources: Iterable[RegisteredResource],
    *,
    service_name: str,
    service_version: str,
    oidc_discovery_url: str | None = None,
    stats_ttl: int | None = None,
) -> Manifest:
    """Build the manifest of `resources`, ordered by name then api_version.

    The caller has already been filtered out of `resources`; this renders what it is given.
    `stats_ttl` is the host's `/stats` cache TTL in seconds (the library cannot see it),
    reported only for resources that serve stats.
    """
    ordered = sorted(resources, key=lambda r: r.identity)
    return Manifest(
        manifest_version=MANIFEST_VERSION,
        service=ServiceInfo(name=service_name, version=service_version),
        resources=[
            _resource_manifest(r, oidc_discovery_url=oidc_discovery_url, stats_ttl=stats_ttl)
            for r in ordered
        ],
    )
