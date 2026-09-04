"""Generic XML (de)serialization for flat Pydantic models.

Works with any BaseModel whose fields are all scalars (no nested models/lists) --
Hero fits that shape (see app.controllers.heroes_xml for the applied example). Uses
stdlib xml.etree.ElementTree rather than a third-party XML library: a flat model
needs nothing more than one element per field.
"""

from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from pydantic import BaseModel


def to_xml(model: BaseModel, root_tag: str) -> str:
    """Render a flat Pydantic model as an XML document, one child element per field."""
    root = Element(root_tag)
    for field, value in model.model_dump(mode="json").items():
        child = SubElement(root, field)
        child.text = str(value)
    return tostring(root, encoding="unicode")


def from_xml[ModelT: BaseModel](body: bytes, schema: type[ModelT]) -> ModelT:
    """Parse an XML document (one child element per field) into the given model."""
    root = fromstring(body)  # noqa: S314 -- request body, not untrusted external XML with entities
    data = {child.tag: child.text for child in root}
    return schema.model_validate(data)
