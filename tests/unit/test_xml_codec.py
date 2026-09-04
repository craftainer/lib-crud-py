"""Unit test: to_xml/from_xml round-trip a flat Pydantic model through XML."""

from datetime import UTC, datetime

from app.views.hero import Hero, HeroCreate
from app.xml_codec import from_xml, to_xml


def test_to_xml_renders_one_element_per_field() -> None:
    """to_xml renders a flat model as one child element per field, under root_tag."""
    hero = HeroCreate(name="Spider-Man", superpower="Wall-crawling")
    xml = to_xml(hero, "hero")
    assert xml == "<hero><name>Spider-Man</name><superpower>Wall-crawling</superpower></hero>"


def test_from_xml_parses_fields_into_the_given_schema() -> None:
    """from_xml parses an XML document's child elements into the given model."""
    xml = b"<hero><name>Batman</name><superpower>Detective skills</superpower></hero>"
    hero = from_xml(xml, HeroCreate)
    assert hero == HeroCreate(name="Batman", superpower="Detective skills")


def test_xml_round_trips_through_to_xml_and_from_xml() -> None:
    """A model serialized with to_xml and parsed back with from_xml is unchanged."""
    original = HeroCreate(name="Wonder Woman", superpower="Super strength")
    parsed = from_xml(to_xml(original, "hero").encode(), HeroCreate)
    assert parsed == original


def test_to_xml_stringifies_non_string_fields() -> None:
    """to_xml renders non-string field values (e.g. int id) as their string form."""

    hero = Hero(
        id=1,
        name="Batman",
        superpower="Detective skills",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    xml = to_xml(hero, "hero")
    assert "<id>1</id>" in xml
