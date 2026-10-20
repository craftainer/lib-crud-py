"""What the router factories tell the manifest about each resource they build.

A host creates one `ManifestRegistry`, passes it to every CRUD router factory through
`ManifestOptions`, and hands the same registry to `crud_manifest_router`. The factory
records what it already knows (schemas, which optional routes it mounted, the role
dependencies) at build time, so the manifest describes the routers that exist rather
than a second list someone has to keep in step.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisteredResource:
    """One resource version as a router factory built it. Everything the manifest needs."""

    name: str
    title: str
    api_version: str
    base_path: str
    formats: dict[str, str]
    schema: type[BaseModel]
    create_schema: type[BaseModel]
    update_schema: type[BaseModel]
    draft_schema: type[BaseModel] | None
    archivable: bool
    revisions: bool
    events_resource: str | None
    stats: bool
    schedulable: bool
    owner_scoped: bool
    public_read: bool
    read_gate: Any
    """The router's own `read_roles` dependency (`Depends(...)`), evaluated per caller."""
    read_roles: tuple[str, ...]
    write_roles: tuple[str, ...]
    delete_roles: tuple[str, ...]

    @property
    def identity(self) -> tuple[str, str]:
        """`(name, api_version)`: what makes two registered resources the same one."""
        return (self.name, self.api_version)


class ManifestRegistry:
    """The resources a host's CRUD routers have registered, in registration order."""

    def __init__(self) -> None:
        """Start empty."""
        self._resources: dict[tuple[str, str], RegisteredResource] = {}

    def register(self, resource: RegisteredResource) -> None:
        """Add `resource`; a second registration of the same `(name, api_version)` is an error."""
        if resource.identity in self._resources:
            msg = f"resource {resource.name!r} {resource.api_version} is already registered"
            raise ValueError(msg)
        self._resources[resource.identity] = resource

    def resources(self) -> tuple[RegisteredResource, ...]:
        """Return every registered resource."""
        return tuple(self._resources.values())


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestOptions:
    """What a host adds to a router factory call so the router appears in the manifest.

    `name` + `api_version` identify the resource (`heroes`, `v2`); v1 and v2 of one model
    are two registrations. `model` is the persistence model: `capabilities.schedulable`
    is read from it (`issubclass(model, Schedulable)`). `owner_scoped` says callers see
    only records they own, which the factory cannot see inside the opaque CRUD
    dependency. The role names describe the router's `read_roles`/`write_roles`/
    `delete_roles` dependencies for display; when omitted they fall back to a `roles`
    attribute on the dependency callable. `public_read` marks a resource anyone may
    read (the manifest then lists it for anonymous callers and its `read_roles` stay
    empty). `base_path` is the JSON collection URL, needed only for `build_json_router`
    (`build_resource_router` derives it from its `api_prefix`).
    """

    registry: ManifestRegistry
    name: str
    api_version: str
    title: str | None = None
    model: type | None = None
    owner_scoped: bool = False
    public_read: bool = False
    read_roles: Sequence[str] | None = None
    write_roles: Sequence[str] | None = None
    delete_roles: Sequence[str] | None = None
    base_path: str | None = None
