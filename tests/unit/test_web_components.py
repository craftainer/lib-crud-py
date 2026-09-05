"""Unit test: the generic HTML form and web-component JS templates."""

from app.web_components import render_crud_component_js, render_crud_form


def test_render_crud_form_includes_an_input_per_field() -> None:
    """render_crud_form renders one <input> per field, plus the components script tag."""
    html = render_crud_form("hero", ["name", "powers"], "/heroes")
    assert '<input name="name" required>' in html
    assert '<input name="powers" required>' in html
    assert '<script src="/heroes/components.js">' in html
    assert "<hero-list" in html


def test_render_crud_component_js_defines_custom_elements() -> None:
    """render_crud_component_js registers <resource-list> and <resource-form>."""
    js = render_crud_component_js("hero", "/heroes", ["name", "powers"])
    assert 'customElements.define("hero-list", HeroList);' in js
    assert 'customElements.define("hero-form", HeroForm);' in js
    assert "/heroes" in js


def test_render_crud_component_js_fetches_filters_metadata_on_connect() -> None:
    """The list element fetches `${apiBase}/filters` once, on connectedCallback."""
    js = render_crud_component_js("hero", "/heroes", ["name", "powers"])
    assert "await fetch(`${this.apiBase}/filters`)" in js
    assert "connectedCallback" in js


def test_render_crud_component_js_bulk_actions_use_id_in_filter() -> None:
    """Bulk delete/update target exactly the checked rows via an `id__in=` filter."""
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert '`${this.apiBase}?id__in=${ids.join(",")}`' in js
    assert 'method: "DELETE"' in js
    assert "bulk-delete" in js
    assert "bulk-edit" in js


def test_render_crud_component_js_single_delete_uses_id_query_param() -> None:
    """A row's own delete button targets `?id=`, not a path segment."""
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert "`${this.apiBase}?id=${button.dataset.id}`" in js


def test_render_crud_component_js_with_list_fields_splits_and_joins() -> None:
    """A list_fields entry gets split on "," in the form handler and joined for display."""
    js = render_crud_component_js("hero", "/heroes", ["name", "powers"], list_fields=["powers"])
    assert 'data[f] = data[f].split(",").map(v => v.trim()).filter(v => v);' in js
    assert 'listFields.includes(f) ? record[f].join(", ") : record[f]' in js


def test_render_crud_component_js_escapes_field_values_before_interpolation() -> None:
    """List.refresh() runs every displayed field value through escapeHtml, not raw innerHTML."""
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert "escapeHtml(display(record, f))" in js
    assert "escapeHtml(record.id)" in js

    escape_html_js = """function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}"""
    assert escape_html_js in js
    assert escape_html("<img src=x onerror=alert(1)>") == "&lt;img src=x onerror=alert(1)&gt;"


def escape_html(value: str) -> str:
    """Reimplement the generated JS's escapeHtml in Python, to assert its escaping behavior."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
