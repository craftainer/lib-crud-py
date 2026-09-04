"""Generic XML (de)serialization for flat Pydantic models.

Works with any BaseModel whose fields are scalars or flat lists of scalars (no
nested models) -- Hero fits that shape (see app.controllers.heroes_xml for the
applied example). Uses stdlib xml.etree.ElementTree rather than a third-party XML
library: a flat model needs nothing more than one element per field (repeated for a
list field, one element per item).
"""

from typing import Annotated, Union, get_args, get_origin
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from pydantic import BaseModel


def _is_list_annotation(annotation: object) -> bool:
    """Return whether a Pydantic field annotation is a list, seeing through the
    Annotated/`| None` wrappers HeroUpdate-style optional fields add around it.
    """
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin is Annotated:
        return _is_list_annotation(get_args(annotation)[0])
    if origin is Union:
        return any(
            _is_list_annotation(arg) for arg in get_args(annotation) if arg is not type(None)
        )
    return False


def to_xml(model: BaseModel, root_tag: str) -> str:
    """Render a flat Pydantic model as XML: one child element per scalar field, one
    repeated child element per item of a list field.
    """
    root = Element(root_tag)
    for field, value in model.model_dump(mode="json").items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            child = SubElement(root, field)
            child.text = str(item)
    return tostring(root, encoding="unicode")


def from_xml[ModelT: BaseModel](body: bytes, schema: type[ModelT]) -> ModelT:
    """Parse an XML document (repeated elements group into a list field, single
    elements stay scalar) into the given model.
    """
    root = fromstring(body)  # noqa: S314 -- request body, not untrusted external XML with entities
    grouped: dict[str, list[str | None]] = {}
    for child in root:
        grouped.setdefault(child.tag, []).append(child.text)
    data: dict[str, str | list[str | None] | None] = {}
    for tag, values in grouped.items():
        field = schema.model_fields.get(tag)
        is_list_field = field is not None and _is_list_annotation(field.annotation)
        data[tag] = values if is_list_field else values[-1]
    return schema.model_validate(data)
