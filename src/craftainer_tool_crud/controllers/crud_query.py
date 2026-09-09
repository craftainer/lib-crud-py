"""Query-string parsing for the generic CRUD router factories.

Every filter/sort rule a resource's routes accept is derived mechanically from
that resource's own Pydantic schema -- a numeric field gets range/membership
operators, a string field gets substring/regex operators, a bool/Enum/Literal
field gets equality/membership, and nothing else. `field_specs`/`describe_fields`
are the single source of truth for this per-field-type mapping: `parse_filters`/
`parse_sort` use it to validate and parse incoming query strings, and
`describe_fields` exposes the same information as JSON for a web UI to render
filter controls from (see crud.controllers.crud_router's `/filters` route).

Wire format: `field=value` is always an equality match; `field__min=`/
`field__max=` express a numeric/date/datetime range; `field__in=a,b,c` is
membership; `field__contains=`/`field__icontains=`/`field__regex=` are string
operators. `sort=field,-other_field` is comma-separated field names, a leading
`-` meaning descending.

A few branches below are `# pragma: no cover`: this module's classification is
generic over every field kind a Pydantic schema can have, but Hero (the only
resource wired up through the HTTP layer that `tests/integration`/`tests/e2e`
exercise) has no Enum, Literal, or plain-`date` field in its *read* view (it does
now carry Optional and bool fields, from the record-lifecycle mixins -- see
`crud.models.mixins` -- so those branches are exercised for real), so those
remaining branches can never run through the real HTTP stack.
`tests/unit/controllers/test_crud_query.py` exercises every one of them
directly against a standalone schema built for that purpose -- the pragma only
affects what's counted toward the integration/e2e coverage gates, not whether
these lines run there.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.datastructures import QueryParams

from crud.repositories.filtering import FilterClause, FilterOp, SortClause

_RESERVED_PARAMS = frozenset({"skip", "limit", "sort", "include_archived", "include_unpublished"})

# A `field__regex=` filter reaches Postgres's `~` operator (SQLAlchemyRepository) or
# Python's re.search (InMemoryRepository) verbatim -- an unbounded pattern is a ReDoS
# vector via catastrophic backtracking (e.g. "(a+)+$" against a crafted string, only
# 10 characters). Pattern *length* doesn't bound that -- backtracking cost is driven
# by structure, not size -- so this cap is only a sanity limit against a genuinely
# huge pattern; the actual ReDoS defense is a runtime budget enforced where the
# pattern is evaluated (crud.repositories.memory's `_regex_matches` alarm timeout,
# crud.repositories.sqlalchemy's per-transaction `statement_timeout`).
_MAX_REGEX_PATTERN_LENGTH = 200


class FieldKind:
    """String constants for the "kind" of a filterable field, as exposed over the wire."""

    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"
    ENUM = "enum"


_OPS_BY_KIND: dict[str, tuple[str, ...]] = {
    FieldKind.NUMBER: ("eq", "min", "max", "in"),
    FieldKind.STRING: ("eq", "contains", "icontains", "regex"),
    FieldKind.BOOLEAN: ("eq", "in"),
    FieldKind.ENUM: ("eq", "in"),
}

_SUFFIX_TO_OP: dict[str, FilterOp] = {
    "eq": FilterOp.EQ,
    "min": FilterOp.GTE,
    "max": FilterOp.LTE,
    "in": FilterOp.IN,
    "contains": FilterOp.CONTAINS,
    "icontains": FilterOp.ICONTAINS,
    "regex": FilterOp.REGEX,
}


@dataclass(frozen=True)
class FieldSpec:
    """What a schema field can be filtered by: its wire "kind", valid ops, and Python type."""

    kind: str
    ops: tuple[str, ...]
    python_type: Any
    choices: tuple[str, ...] | None = None


class FieldFilterInfo(BaseModel):
    """One field's filter/sort description, as served by the `/filters` metadata route."""

    name: str
    kind: str
    ops: list[str]
    choices: list[str] | None = None


def _unwrap_optional(annotation: object) -> object:
    """Peel an `X | None` wrapper down to the underlying type `X`.

    Pydantic's `FieldInfo.annotation` already strips `Annotated[...]` metadata (a
    field declared `Annotated[datetime, ...]` reports its annotation as plain
    `datetime`), so only `X | None` needs unwrapping here.
    """
    if get_origin(annotation) is Union:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:  # pragma: no cover -- see module docstring
            return args[0]
    return annotation


_KIND_BY_BASE: tuple[tuple[type, str], ...] = (
    (bool, FieldKind.BOOLEAN),
    (int, FieldKind.NUMBER),
    (float, FieldKind.NUMBER),
    (date, FieldKind.NUMBER),
    (datetime, FieldKind.NUMBER),
    (Enum, FieldKind.ENUM),
    (str, FieldKind.STRING),
)


def _classify(annotation: object) -> str | None:
    """Return the wire "kind" for a field annotation, or None if it isn't filterable."""
    resolved = _unwrap_optional(annotation)
    if get_origin(resolved) is Literal:  # pragma: no cover -- see module docstring
        return FieldKind.ENUM
    if not isinstance(resolved, type):
        return None
    return next((kind for base, kind in _KIND_BY_BASE if issubclass(resolved, base)), None)


def _choices(kind: str, python_type: object) -> tuple[str, ...] | None:
    if kind != FieldKind.ENUM:
        return None
    # Hero has no Enum/Literal field, so `kind` is never "enum" through the real HTTP
    # stack -- see module docstring.
    if isinstance(python_type, type) and issubclass(python_type, Enum):  # pragma: no cover
        return tuple(str(member.value) for member in python_type)
    return tuple(str(arg) for arg in get_args(python_type))  # pragma: no cover


def field_specs(schema: type[BaseModel]) -> dict[str, FieldSpec]:
    """Map each filterable field of `schema` to its FieldSpec; non-filterable fields are omitted."""
    specs: dict[str, FieldSpec] = {}
    for name, info in schema.model_fields.items():
        kind = _classify(info.annotation)
        if kind is None:
            continue
        python_type = _unwrap_optional(info.annotation)
        specs[name] = FieldSpec(
            kind=kind,
            ops=_OPS_BY_KIND[kind],
            python_type=python_type,
            choices=_choices(kind, python_type),
        )
    return specs


def describe_fields(schema: type[BaseModel]) -> list[FieldFilterInfo]:
    """Describe every filterable field of `schema`, for the `/filters` metadata route."""
    return [
        FieldFilterInfo(
            name=name, kind=spec.kind, ops=list(spec.ops), choices=list(spec.choices or ()) or None
        )
        for name, spec in field_specs(schema).items()
    ]


def _cast_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise ValueError(raw)


def _cast_datetime(raw: str) -> datetime:
    """Parse an ISO datetime, normalizing a tz-aware value to naive UTC.

    Every timestamp this app stores is naive-but-conceptually-UTC (see
    crud.models.base.IdentifiedBase / crud.views.base.IXDTFDatetime), so a
    tz-aware filter value (e.g. "...+00:00" or "...Z") needs converting the
    same way before it can be compared against that column -- the DB driver
    otherwise rejects mixing naive and aware datetimes outright.
    """
    value = datetime.fromisoformat(raw)
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def _cast_number(python_type: object, raw: str) -> object:
    if isinstance(python_type, type) and issubclass(python_type, datetime):
        return _cast_datetime(raw)
    if isinstance(python_type, type) and issubclass(python_type, date):  # pragma: no cover
        return date.fromisoformat(raw)  # see module docstring: Hero has no plain-date field
    assert callable(python_type)  # noqa: S101 -- python_type is always int or float here
    return python_type(raw)


def _cast_enum(python_type: object, raw: str) -> object:  # pragma: no cover -- see module docstring
    if isinstance(python_type, type) and issubclass(python_type, Enum):
        return python_type(raw)
    return raw


_CASTERS: dict[str, Any] = {
    FieldKind.BOOLEAN: lambda spec, raw: _cast_bool(raw),
    FieldKind.NUMBER: lambda spec, raw: _cast_number(spec.python_type, raw),
    FieldKind.ENUM: lambda spec, raw: _cast_enum(spec.python_type, raw),
    FieldKind.STRING: lambda spec, raw: raw,
}


def _cast(spec: FieldSpec, raw: str) -> object:
    """Convert one raw query-string value to the Python type `spec.python_type` expects."""
    return _CASTERS[spec.kind](spec, raw)


def _split_key(key: str) -> tuple[str, str]:
    """Split a query key into (field, op-suffix), defaulting to the "eq" suffix."""
    field, sep, suffix = key.rpartition("__")
    return (field, suffix) if sep else (key, "eq")


def parse_filters(schema: type[BaseModel], params: QueryParams) -> list[FilterClause]:
    """Parse every non-reserved query param into a FilterClause, validated against `schema`.

    An unrecognized field name, an operator not valid for that field's type, or a
    value that doesn't parse as that field's type is a 400 (RequestValidationError)
    rather than being silently ignored.
    """
    specs = field_specs(schema)
    clauses: list[FilterClause] = []
    errors: list[dict[str, Any]] = []
    for key, raw in params.multi_items():
        if key in _RESERVED_PARAMS:
            continue
        field, suffix = _split_key(key)
        spec = specs.get(field)
        if spec is None or suffix not in spec.ops:
            errors.append(
                {"loc": ("query", key), "msg": "unrecognized filter", "type": "value_error"}
            )
            continue
        op = _SUFFIX_TO_OP[suffix]
        if op is FilterOp.REGEX and len(raw) > _MAX_REGEX_PATTERN_LENGTH:
            errors.append(
                {"loc": ("query", key), "msg": "regex pattern too long", "type": "value_error"}
            )
            continue
        try:
            value = (
                [_cast(spec, v) for v in raw.split(",")] if op is FilterOp.IN else _cast(spec, raw)
            )
        except ValueError:
            errors.append(
                {"loc": ("query", key), "msg": "invalid filter value", "type": "value_error"}
            )
            continue
        clauses.append(FilterClause(field, op, value))
    if errors:
        raise RequestValidationError(errors)
    return clauses


def parse_sort(schema: type[BaseModel], params: QueryParams) -> list[SortClause]:
    """Parse a `sort=a,-b` query param into SortClauses, validated against `schema`."""
    raw = params.get("sort")
    if not raw:
        return []
    specs = field_specs(schema)
    clauses: list[SortClause] = []
    errors: list[dict[str, Any]] = []
    for raw_part in raw.split(","):
        part = raw_part.strip()
        if not part:
            continue
        descending = part.startswith("-")
        field = part[1:] if descending else part
        if field not in specs:
            errors.append(
                {
                    "loc": ("query", "sort"),
                    "msg": f"unrecognized field {field!r}",
                    "type": "value_error",
                }
            )
            continue
        clauses.append(SortClause(field, descending=descending))
    if errors:
        raise RequestValidationError(errors)
    return clauses


def _parse_bool_flag(params: QueryParams, key: str) -> bool:
    """Parse a `?<key>=true|false` boolean query flag, defaulting to False when absent.

    Shares `_cast_bool`'s "true"/"1"/"false"/"0" vocabulary; an unrecognized value
    is a 400 (RequestValidationError), matching every other filter value's own
    invalid-value handling in `parse_filters`.
    """
    raw = params.get(key)
    if raw is None:
        return False
    try:
        return _cast_bool(raw)
    except ValueError as exc:
        errors = [{"loc": ("query", key), "msg": "invalid filter value", "type": "value_error"}]
        raise RequestValidationError(errors) from exc


def parse_include_archived(params: QueryParams) -> bool:
    """Parse the `?include_archived=true` flag Archivable list/get routes accept.

    See crud.repositories.sqlalchemy/crud.repositories.memory's default-exclusion
    logic for what this flag overrides.
    """
    return _parse_bool_flag(params, "include_archived")


def parse_include_unpublished(params: QueryParams) -> bool:
    """Parse the `?include_unpublished=true` flag Schedulable list/get routes accept.

    See crud.repositories.sqlalchemy/crud.repositories.memory's default-exclusion
    logic for what this flag overrides.
    """
    return _parse_bool_flag(params, "include_unpublished")


__all__: Sequence[str] = (
    "FieldFilterInfo",
    "FieldSpec",
    "describe_fields",
    "field_specs",
    "parse_filters",
    "parse_include_archived",
    "parse_include_unpublished",
    "parse_sort",
)
