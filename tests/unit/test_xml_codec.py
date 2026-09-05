"""Unit test: to_xml/from_xml round-trip a flat Pydantic model, scalars and lists, through XML."""

from datetime import UTC, datetime

from app.views.hero_v2 import HeroV2, HeroV2Create
from app.xml_codec import from_xml, to_xml


def test_to_xml_renders_one_element_per_scalar_field() -> None:
    """to_xml renders a scalar field as one child element, under root_tag."""
    hero = HeroV2Create(name="Spider-Man", powers=["Wall-crawling"])
    xml = to_xml(hero, "hero")
    assert "<name>Spider-Man</name>" in xml


def test_to_xml_renders_one_element_per_list_item() -> None:
    """to_xml renders a list field as one repeated child element per item."""
    hero = HeroV2Create(name="Storm", powers=["Weather control", "Flight"])
    xml = to_xml(hero, "hero")
    assert "<powers>Weather control</powers><powers>Flight</powers>" in xml


def test_from_xml_parses_fields_into_the_given_schema() -> None:
    """from_xml parses an XML document's child elements into the given model."""
    xml = b"<hero><name>Batman</name><powers>Detective skills</powers></hero>"
    hero = from_xml(xml, HeroV2Create)
    assert hero == HeroV2Create(name="Batman", powers=["Detective skills"])


def test_from_xml_groups_repeated_elements_into_a_list() -> None:
    """from_xml groups multiple same-tag elements into a list field."""
    xml = b"<hero><name>Storm</name><powers>Weather control</powers><powers>Flight</powers></hero>"
    hero = from_xml(xml, HeroV2Create)
    assert hero == HeroV2Create(name="Storm", powers=["Weather control", "Flight"])


def test_from_xml_keeps_a_single_element_list_field_as_a_list() -> None:
    """A list field with exactly one XML element still validates as a list, not a scalar."""
    xml = b"<hero><name>Batman</name><powers>Detective skills</powers></hero>"
    hero = from_xml(xml, HeroV2Create)
    assert hero.powers == ["Detective skills"]


def test_xml_round_trips_through_to_xml_and_from_xml() -> None:
    """A model serialized with to_xml and parsed back with from_xml is unchanged, including
    mixed scalar and list fields.
    """
    original = HeroV2Create(name="Wonder Woman", powers=["Super strength", "Flight"])
    parsed = from_xml(to_xml(original, "hero").encode(), HeroV2Create)
    assert parsed == original


def test_to_xml_stringifies_non_string_fields() -> None:
    """to_xml renders non-string field values (e.g. int id) as their string form."""
    hero = HeroV2(
        id=1,
        name="Batman",
        powers=["Detective skills"],
        owner_id="alice",
        is_draft=False,
        archived_at=None,
        publish_at=None,
        unpublish_at=None,
        is_locked=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    xml = to_xml(hero, "hero")
    assert "<id>1</id>" in xml
