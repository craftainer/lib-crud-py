"""Unit test: crud_query.py's schema-driven filter/sort query-string parsing.

Uses a small standalone Pydantic schema covering every field kind (number,
string, boolean, enum), not tied to Hero, to prove the parsing is genuinely
schema-driven.
"""

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.datastructures import QueryParams

from crud.controllers.crud_query import (
    describe_fields,
    parse_filters,
    parse_include_archived,
    parse_include_unpublished,
    parse_sort,
)
from crud.repositories.filtering import FilterClause, FilterOp, SortClause


class _Status(Enum):
    """Plain enum field, to exercise the enum-classification/casting/choices paths."""

    OPEN = "open"
    CLOSED = "closed"


class _Widget(BaseModel):
    """Standalone schema covering every filterable field kind."""

    id: int
    label: str
    active: bool
    status: str
    powers: list[str]
    stage: _Status
    priority: Literal["low", "high"]
    due_on: date
    created_at: Annotated[datetime, "tag"]
    nickname: str | None = None
    mixed: int | str | None = None


def test_describe_fields_skips_list_fields() -> None:
    """describe_fields() omits fields whose kind can't be classified (e.g. list fields)."""
    names = {info.name for info in describe_fields(_Widget)}
    assert "powers" not in names


def test_describe_fields_reports_ops_per_kind() -> None:
    """Each field's description lists exactly the ops valid for its kind."""
    by_name = {info.name: info for info in describe_fields(_Widget)}
    assert by_name["id"].kind == "number"
    assert set(by_name["id"].ops) == {"eq", "min", "max", "in"}
    assert by_name["label"].kind == "string"
    assert set(by_name["label"].ops) == {"eq", "contains", "icontains", "regex"}
    assert by_name["active"].kind == "boolean"
    assert set(by_name["active"].ops) == {"eq", "in"}


def test_describe_fields_enum_reports_choices() -> None:
    """An Enum field is described as "enum" with its member values as choices."""
    by_name = {info.name: info for info in describe_fields(_Widget)}
    assert by_name["stage"].kind == "enum"
    assert set(by_name["stage"].ops) == {"eq", "in"}
    assert set(by_name["stage"].choices or []) == {"open", "closed"}


def test_describe_fields_literal_reports_choices() -> None:
    """A Literal field is described as "enum" with its literal values as choices."""
    by_name = {info.name: info for info in describe_fields(_Widget)}
    assert by_name["priority"].kind == "enum"
    assert set(by_name["priority"].choices or []) == {"low", "high"}


def test_describe_fields_date_and_annotated_datetime_are_number_kind() -> None:
    """A `date` field and an `Annotated[datetime, ...]` field both classify as "number"."""
    by_name = {info.name: info for info in describe_fields(_Widget)}
    assert by_name["due_on"].kind == "number"
    assert by_name["created_at"].kind == "number"


def test_describe_fields_optional_field_unwraps_to_its_inner_kind() -> None:
    """A `str | None` field classifies by its inner type, same as a required `str` field."""
    by_name = {info.name: info for info in describe_fields(_Widget)}
    assert by_name["nickname"].kind == "string"


def test_parse_filters_enum_field() -> None:
    """An Enum field's bare value is cast to the enum member by value."""
    clauses = parse_filters(_Widget, QueryParams("stage=open"))
    assert clauses == [FilterClause("stage", FilterOp.EQ, _Status.OPEN)]


def test_parse_filters_literal_field() -> None:
    """A Literal field's bare value is kept as the raw string."""
    clauses = parse_filters(_Widget, QueryParams("priority=high"))
    assert clauses == [FilterClause("priority", FilterOp.EQ, "high")]


def test_parse_filters_date_field() -> None:
    """A `date` field's value is parsed via date.fromisoformat."""
    clauses = parse_filters(_Widget, QueryParams("due_on__min=2026-01-01"))
    assert clauses == [FilterClause("due_on", FilterOp.GTE, date(2026, 1, 1))]


def test_parse_filters_annotated_datetime_field() -> None:
    """An `Annotated[datetime, ...]` field's value is parsed via datetime.fromisoformat."""
    clauses = parse_filters(_Widget, QueryParams("created_at__min=2026-01-01T00:00:00"))
    assert clauses == [FilterClause("created_at", FilterOp.GTE, datetime(2026, 1, 1))]


def test_parse_filters_datetime_field_normalizes_tz_aware_value_to_naive_utc() -> None:
    """A tz-aware datetime filter value is converted to naive UTC before comparison.

    Every timestamp this app stores is naive-but-conceptually-UTC (see
    crud.models.base.IdentifiedBase), so a "+00:00"/"Z"-suffixed filter value has to
    be normalized the same way, or the DB driver rejects comparing it to a naive
    column outright.
    """
    clauses = parse_filters(_Widget, QueryParams("created_at__min=2026-01-01T02:00:00%2B02:00"))
    assert clauses == [FilterClause("created_at", FilterOp.GTE, datetime(2026, 1, 1))]


def test_parse_filters_bare_equals() -> None:
    """A bare `field=value` query key parses as an EQ filter."""
    clauses = parse_filters(_Widget, QueryParams("label=apple"))
    assert clauses == [FilterClause("label", FilterOp.EQ, "apple")]


def test_parse_filters_numeric_range() -> None:
    """`field__min=`/`field__max=` parse as GTE/LTE filters, cast to the field's type."""
    clauses = parse_filters(_Widget, QueryParams("id__min=1&id__max=10"))
    assert set(clauses) == {
        FilterClause("id", FilterOp.GTE, 1),
        FilterClause("id", FilterOp.LTE, 10),
    }


def test_parse_filters_membership() -> None:
    """`field__in=a,b,c` splits on comma and parses as an IN filter."""
    clauses = parse_filters(_Widget, QueryParams("id__in=1,2,3"))
    assert clauses == [FilterClause("id", FilterOp.IN, [1, 2, 3])]


def test_parse_filters_string_ops() -> None:
    """String fields accept contains/icontains/regex, in addition to bare equality."""
    clauses = parse_filters(_Widget, QueryParams("label__icontains=App"))
    assert clauses == [FilterClause("label", FilterOp.ICONTAINS, "App")]


def test_parse_filters_regex_within_length_limit_accepted() -> None:
    """A `field__regex=` filter at or under the length cap parses normally."""
    pattern = "a" * 200
    clauses = parse_filters(_Widget, QueryParams(f"label__regex={pattern}"))
    assert clauses == [FilterClause("label", FilterOp.REGEX, pattern)]


def test_parse_filters_regex_over_length_limit_rejected() -> None:
    """A `field__regex=` filter over the length cap is a 400, guarding against ReDoS."""
    pattern = "a" * 201
    with pytest.raises(RequestValidationError):
        parse_filters(_Widget, QueryParams(f"label__regex={pattern}"))


def test_parse_filters_boolean() -> None:
    """A boolean field's bare value parses true/false (case-insensitively)."""
    assert parse_filters(_Widget, QueryParams("active=True")) == [
        FilterClause("active", FilterOp.EQ, True)
    ]
    assert parse_filters(_Widget, QueryParams("active=false")) == [
        FilterClause("active", FilterOp.EQ, False)
    ]


def test_parse_filters_invalid_boolean_rejected() -> None:
    """A boolean field's value that isn't true/false/1/0 is a 400."""
    with pytest.raises(RequestValidationError):
        parse_filters(_Widget, QueryParams("active=maybe"))


def test_parse_filters_unknown_field_rejected() -> None:
    """A query key referencing a field that doesn't exist on the schema is a 400."""
    with pytest.raises(RequestValidationError):
        parse_filters(_Widget, QueryParams("nonexistent=1"))


def test_parse_filters_invalid_operator_for_type_rejected() -> None:
    """An operator not valid for a field's kind (e.g. `__contains` on a number) is a 400."""
    with pytest.raises(RequestValidationError):
        parse_filters(_Widget, QueryParams("id__contains=1"))


def test_parse_filters_invalid_value_rejected() -> None:
    """A value that doesn't parse as the field's type is a 400."""
    with pytest.raises(RequestValidationError):
        parse_filters(_Widget, QueryParams("id=not-a-number"))


def test_parse_filters_ignores_reserved_params() -> None:
    """skip/limit/sort are reserved and never parsed as filters."""
    clauses = parse_filters(_Widget, QueryParams("skip=1&limit=2&sort=id"))
    assert clauses == []


def test_parse_sort_ascending_and_descending() -> None:
    """A leading `-` marks a sort field descending; otherwise ascending."""
    clauses = parse_sort(_Widget, QueryParams("sort=label,-id"))
    assert clauses == [SortClause("label"), SortClause("id", descending=True)]


def test_parse_sort_missing_returns_empty() -> None:
    """No `sort` query param means no sort clauses."""
    assert parse_sort(_Widget, QueryParams("")) == []


def test_parse_sort_unknown_field_rejected() -> None:
    """Sorting by a field that doesn't exist on the schema is a 400."""
    with pytest.raises(RequestValidationError):
        parse_sort(_Widget, QueryParams("sort=nonexistent"))


def test_parse_sort_skips_blank_segments() -> None:
    """A blank segment from a trailing/doubled comma in `sort=` is ignored, not an error."""
    assert parse_sort(_Widget, QueryParams("sort=label,")) == [SortClause("label")]


def test_describe_fields_multi_arg_union_is_unclassifiable() -> None:
    """A Union with more than one non-None member (not a plain `X | None`) is skipped,
    same as any other field kind `_classify` can't map -- `_unwrap_optional` only
    unwraps a single-member union, leaving a multi-member one as-is and therefore
    not a `type` `_classify` can `issubclass()`-check.
    """
    assert "mixed" not in {info.name for info in describe_fields(_Widget)}


def test_parse_include_archived_true_and_false() -> None:
    """?include_archived=true/false parses to the corresponding bool; absent is False."""
    assert parse_include_archived(QueryParams("include_archived=true")) is True
    assert parse_include_archived(QueryParams("include_archived=false")) is False
    assert parse_include_archived(QueryParams("")) is False


def test_parse_include_unpublished_true_and_false() -> None:
    """?include_unpublished=true/false parses to the corresponding bool; absent is False."""
    assert parse_include_unpublished(QueryParams("include_unpublished=true")) is True
    assert parse_include_unpublished(QueryParams("")) is False


def test_parse_include_archived_invalid_value_rejected() -> None:
    """?include_archived=<not true/false/1/0> is a 400, same as an invalid boolean filter."""
    with pytest.raises(RequestValidationError):
        parse_include_archived(QueryParams("include_archived=maybe"))
