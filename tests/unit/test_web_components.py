"""Unit test: the generic HTML form and web-component JS templates."""

from app.web_components import render_crud_component_js, render_crud_form


def test_render_crud_form_includes_an_input_per_field() -> None:
    """render_crud_form renders one <input> per field, plus the components script tag."""
    html = render_crud_form("hero", ["name", "superpower"], "/heroes")
    assert '<input name="name" required>' in html
    assert '<input name="superpower" required>' in html
    assert '<script src="/heroes/components.js">' in html
    assert "<hero-list" in html


def test_render_crud_component_js_defines_custom_elements() -> None:
    """render_crud_component_js registers <resource-list> and <resource-form>."""
    js = render_crud_component_js("hero", "/heroes", ["name", "superpower"])
    assert 'customElements.define("hero-list", HeroList);' in js
    assert 'customElements.define("hero-form", HeroForm);' in js
    assert "/heroes" in js
