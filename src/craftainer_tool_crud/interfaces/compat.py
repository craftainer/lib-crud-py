"""Generic backward-compatibility wrapper around CRUDInterface.

Wraps a current-version CRUDInterface and exposes the same CRUD operations
in terms of an older (deprecated) view, converting responses down to the
legacy shape and incoming payloads up to the current shape via
caller-supplied converter functions -- so a deprecated API version keeps
working against the same repository/current model as the version that
superseded it, without its own duplicated CRUD wiring.
"""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from crud.interfaces.base import CRUDInterface
from crud.repositories.filtering import FilterClause, SortClause


class CompatCRUD[LegacySchemaT: BaseModel, SchemaT: BaseModel, ModelT]:
    """CRUD operations shaped like an older API version, backed by the current CRUDInterface.

    `from_legacy_create`/`from_legacy_update` take `Any` rather than a specific legacy
    payload type: each resource's converter function is typed against its own concrete
    `*Create`/`*Update` view (narrower than `BaseModel`), and `Callable`'s contravariant
    parameter typing would otherwise reject that narrower-than-`BaseModel` signature here.
    """

    def __init__(
        self,
        crud: CRUDInterface[SchemaT, ModelT],
        *,
        to_legacy: Callable[[SchemaT], LegacySchemaT],
        from_legacy_create: Callable[[Any], BaseModel],
        from_legacy_update: Callable[[Any], BaseModel],
    ) -> None:
        """Bind this wrapper to the current CRUD it delegates to and its conversion functions."""
        self._crud = crud
        self._to_legacy = to_legacy
        self._from_legacy_create = from_legacy_create
        self._from_legacy_update = from_legacy_update

    async def get(
        self, record_id: int, *, include_archived: bool = False, include_unpublished: bool = False
    ) -> LegacySchemaT | None:
        """Return the record with the given id in the legacy shape, or None.

        `include_archived`/`include_unpublished` are forwarded to the wrapped
        CRUDInterface unconditionally -- see crud.controllers.crud_actions.
        resolve_list_or_get, which passes them on every request regardless of
        whether the underlying model actually carries the matching mixin.
        """
        current = await self._crud.get(
            record_id, include_archived=include_archived, include_unpublished=include_unpublished
        )
        return self._to_legacy(current) if current is not None else None

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 100,
        filters: Sequence[FilterClause] = (),
        sort: Sequence[SortClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> list[LegacySchemaT]:
        """Return up to `limit` matching records in the legacy shape, skipping the first `skip`."""
        items = await self._crud.list(
            skip=skip,
            limit=limit,
            filters=filters,
            sort=sort,
            include_archived=include_archived,
            include_unpublished=include_unpublished,
        )
        return [self._to_legacy(item) for item in items]

    async def count(
        self,
        *,
        filters: Sequence[FilterClause] = (),
        include_archived: bool = False,
        include_unpublished: bool = False,
    ) -> int:
        """Return how many records match the given filters.

        Called by crud.controllers.crud_actions before a bulk update/delete, to cap
        how many records a single action can affect.
        """
        return await self._crud.count(
            filters=filters,
            include_archived=include_archived,
            include_unpublished=include_unpublished,
        )

    async def create(self, data: BaseModel) -> LegacySchemaT:
        """Create a record from a legacy-shaped payload and return it in the legacy shape."""
        created = await self._crud.create(self._from_legacy_create(data))
        return self._to_legacy(created)

    async def update(self, record_id: int, data: BaseModel) -> LegacySchemaT | None:
        """Apply a legacy-shaped partial update, returning the result in the legacy shape."""
        updated = await self._crud.update(record_id, self._from_legacy_update(data))
        return self._to_legacy(updated) if updated is not None else None

    async def delete(self, record_id: int) -> bool:
        """Delete the record with the given id; return whether it existed."""
        return await self._crud.delete(record_id)

    async def update_many(
        self, *, filters: Sequence[FilterClause], data: BaseModel
    ) -> Sequence[LegacySchemaT]:
        """Apply a legacy-shaped partial update to every matching record; return them."""
        updated = await self._crud.update_many(filters=filters, data=self._from_legacy_update(data))
        return [self._to_legacy(item) for item in updated]

    async def delete_many(self, *, filters: Sequence[FilterClause]) -> Sequence[LegacySchemaT]:
        """Delete every record matching the filters; return the records that were deleted."""
        deleted = await self._crud.delete_many(filters=filters)
        return [self._to_legacy(item) for item in deleted]
