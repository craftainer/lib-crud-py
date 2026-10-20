"""Pydantic models of the CRUD manifest v1 document (`schemas/crud-manifest/v1.json`)."""

from typing import Any, Final, Literal

from pydantic import BaseModel

MANIFEST_VERSION: Final = "1"
MANIFEST_PROFILE = "https://craftainer.github.io/schemas/crud-manifest/v1"

ValueType = Literal["integer", "number", "date", "datetime", "string", "boolean", "enum"]
EventAction = Literal["create", "update", "delete", "restore"]


class ServiceInfo(BaseModel):
    """The serving application."""

    name: str
    version: str


class SchemaSet(BaseModel):
    """The JSON Schemas of a resource's records and request bodies."""

    read: dict[str, Any]
    create: dict[str, Any]
    update: dict[str, Any]
    draft: dict[str, Any] | None


class FieldInfo(BaseModel):
    """One filterable and sortable field."""

    name: str
    value_type: ValueType
    filter_ops: list[str]
    sortable: bool
    choices: list[str] | None
    numeric: bool
    categorical: bool
    read_only: bool
    required: bool


class Pagination(BaseModel):
    """Offset pagination as the list route implements it."""

    style: Literal["offset"]
    skip_param: str
    limit_param: str
    sort_param: str
    default_limit: int
    max_limit: int
    total: bool


class Events(BaseModel):
    """The SSE stream of a resource."""

    transport: Literal["sse"]
    path: str
    resource: str
    resume_param: str
    replay: Literal["persistent", "none"]
    keepalive_seconds: float
    actions: list[EventAction]


class Capabilities(BaseModel):
    """What the resource supports."""

    bulk_update: bool
    bulk_delete: bool
    clone: bool
    draft_publish: bool
    archive_restore: bool
    revisions: bool
    events: Events | None
    stats: bool
    predict: bool
    conditional_requests: bool
    owner_scoped: bool
    schedulable: bool


class Bulk(BaseModel):
    """Limits of bulk PATCH/DELETE."""

    max_matched: int


class Auth(BaseModel):
    """How the resource is protected."""

    scheme: Literal["bearer"]
    oidc_discovery_url: str | None
    public_read: bool
    read_roles: list[str]
    write_roles: list[str]
    delete_roles: list[str]


class Cache(BaseModel):
    """Caching hints."""

    etag: bool
    max_age: int
    stats_ttl: int | None
    stale_while_revalidate: int
    list_etag: Literal["weak", "strong"] | None
    record_etag: Literal["weak", "strong"] | None
    revalidate_only: bool


class ResourceManifest(BaseModel):
    """One resource version."""

    name: str
    title: str
    api_version: str
    base_path: str
    formats: dict[str, str]
    id_field: str
    schemas: SchemaSet
    fields: list[FieldInfo]
    pagination: Pagination
    capabilities: Capabilities
    paths: dict[str, str]
    bulk: Bulk
    auth: Auth
    cache: Cache


class Manifest(BaseModel):
    """The whole document."""

    manifest_version: Literal["1"]
    service: ServiceInfo
    resources: list[ResourceManifest]
