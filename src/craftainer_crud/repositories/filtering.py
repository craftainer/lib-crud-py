"""Storage-agnostic filter/sort vocabulary shared by every Repository implementation.

Plain value objects only -- no SQLAlchemy or Python-eval logic lives here. Each
concrete repository interprets FilterClause/SortClause itself, the same way
Repository stays a Protocol with no shared implementation (see ../README.md).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FilterOp(StrEnum):
    """A comparison a FilterClause applies to one field."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    CONTAINS = "contains"
    ICONTAINS = "icontains"
    REGEX = "regex"  # Postgres-specific (`~` operator); not portable to other SQL backends.


@dataclass(frozen=True)
class FilterClause:
    """One field/operator/value comparison to apply when listing or bulk-targeting records."""

    field: str
    op: FilterOp
    value: Any


@dataclass(frozen=True)
class SortClause:
    """One field to sort by, ascending unless `descending` is set."""

    field: str
    descending: bool = False
