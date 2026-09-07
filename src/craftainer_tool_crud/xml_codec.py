"""Generic XML (de)serialization for flat Pydantic models.

Works with any BaseModel whose fields are scalars or flat lists of scalars (no
nested models) -- Hero fits that shape (see app.controllers.heroes_xml for the
applied example). Renders with stdlib xml.etree.ElementTree (a flat model needs
nothing more than one element per field, repeated for a list field, one element
per item) but parses with defusedxml -- the request body is untrusted input, and
stdlib ElementTree.fromstring is vulnerable to entity-expansion ("billion laughs")
DoS attacks.
"""

from typing import Annotated, Union, get_args, get_origin
from xml.etree.ElementTree import Element, SubElement, tostring

from defusedxml.ElementTree import fromstring
from pydantic import BaseModel


def is_list_annotation(annotation: object) -> bool:
    """Return whether a Pydantic field annotation is a list, seeing through the
    Annotated/`| None` wrappers HeroV2Update-style optional fields add around it.
    """
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin is Annotated:
        return is_list_annotation(get_args(annotation)[0])
    if origin is Union:
        return any(is_list_annotation(arg) for arg in get_args(annotation) if arg is not type(None))
    return False


def to_xml(model: BaseModel, root_tag: str) -> str:
    """Render a flat Pydantic model as XML: one child element per scalar field, one
    repeated child element per item of a list field. A `None`-valued field is
    omitted entirely (not rendered as the literal text "None") -- from_xml already
    treats a missing tag as "unset" (falling back to the field's own default), so
    this keeps a None field's XML round-trip through to_xml+from_xml lossless, the
    same way an unset field never appears in `model_dump(mode="json")`'s JSON
    sibling response either.
    """
    root = Element(root_tag)
    for field, value in model.model_dump(mode="json").items():
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            child = SubElement(root, field)
            child.text = str(item)
    return tostring(root, encoding="unicode")


def from_xml[ModelT: BaseModel](body: bytes, schema: type[ModelT]) -> ModelT:
    """Parse an XML document (repeated elements group into a list field, single
    elements stay scalar) into the given model.
    """
    root = fromstring(body)
    grouped: dict[str, list[str | None]] = {}
    for child in root:
        grouped.setdefault(child.tag, []).append(child.text)
    data: dict[str, str | list[str | None] | None] = {}
    for tag, values in grouped.items():
        field = schema.model_fields.get(tag)
        is_list_field = field is not None and is_list_annotation(field.annotation)
        data[tag] = values if is_list_field else values[-1]
    return schema.model_validate(data)
