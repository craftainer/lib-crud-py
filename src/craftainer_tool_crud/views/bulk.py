"""Generic result views for the bulk update/delete actions on the CRUD router factories."""

from pydantic import BaseModel


class BulkUpdateResult(BaseModel):
    """Result of a bulk update: how many records matched and which ones were updated."""

    matched: int
    ids: list[int]


class BulkDeleteResult(BaseModel):
    """Result of a bulk delete: how many records matched and which ones were deleted."""

    matched: int
    ids: list[int]
