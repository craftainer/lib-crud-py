"""Unit test: the generic HTML form and web-component JS templates."""

from app.web_components import render_crud_component_js, render_crud_form


def test_render_crud_form_includes_an_input_per_field() -> None:
    """render_crud_form renders one <input> per field, plus the components script tag."""
    html = render_crud_form(
        "hero", ["name", "powers"], "/crud/v1/heroes/v2/json", "/crud/v1/heroes/v2/web"
    )
    assert '<input name="name" required>' in html
    assert '<input name="powers" required>' in html
    assert '<script src="/crud/v1/heroes/v2/web/components.js">' in html
    assert "<hero-list" in html


def test_render_crud_form_escapes_resource_and_field_names() -> None:
    """resource/fields/endpoints are HTML-escaped, defense-in-depth per the module docstring."""
    html = render_crud_form('hero"<script>', ['na"me<'], "/heroes?x=1&y=2", "/heroes")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&quot;" in html
    assert "&amp;" in html


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
    """Bulk delete/update target exactly the checked rows via a URL-encoded `id__in=` filter."""
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert 'new URLSearchParams({ id__in: ids.join(",") })' in js
    assert 'method: "DELETE"' in js
    assert "bulk-delete" in js
    assert "bulk-edit" in js


def test_render_crud_component_js_single_delete_uses_id_query_param() -> None:
    """A row's own delete button targets a URL-encoded `?id=`, not a path segment."""
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert "new URLSearchParams({ id: button.dataset.id })" in js


def test_render_crud_component_js_with_list_fields_splits_and_joins() -> None:
    """A list_fields entry gets split on "," in the form handler and joined for display."""
    js = render_crud_component_js("hero", "/heroes", ["name", "powers"], list_fields=["powers"])
    assert 'data[f] = (data[f] || "").split(",").map(v => v.trim()).filter(v => v);' in js
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


# --- Opt-in capability flags: archivable/draftable/has_revisions/has_events/stats ---


def test_render_crud_component_js_defaults_every_capability_flag_off() -> None:
    """With no opt-in flags passed, every generated capability constant is false.

    The row-action markup itself (restore-row/publish-row/history-row) is still
    present in the generated JS text either way -- it's gated by a runtime `if
    (this.archivable && ...)` check, not omitted from the template -- so this
    only asserts the *constants* the runtime checks are false.
    """
    js = render_crud_component_js("hero", "/heroes", ["name"])
    assert "this.archivable = false;" in js
    assert "this.draftable = false;" in js
    assert "this.hasRevisions = false;" in js
    assert "this.hasEvents = false;" in js
    assert "this.statsEnabled = false;" in js


def test_render_crud_component_js_archivable_adds_restore_action() -> None:
    """archivable=True renders an "include archived" toggle and a Restore row action."""
    js = render_crud_component_js("hero", "/heroes", ["name"], archivable=True)
    assert "this.archivable = true;" in js
    assert "include-archived" in js
    assert "restore-row" in js
    assert "/restore?" in js


def test_render_crud_component_js_draftable_adds_publish_action_and_draft_button() -> None:
    """draftable=True renders a Publish row action and a Save-as-draft form button."""
    js = render_crud_component_js("hero", "/heroes", ["name"], draftable=True)
    assert "this.draftable = true;" in js
    assert "publish-row" in js
    assert "/publish?" in js
    assert "Save as draft" in js
    assert "/draft`" in js


def test_render_crud_component_js_has_revisions_adds_history_panel() -> None:
    """has_revisions=True renders a History row action fetching GET .../revisions?id=."""
    js = render_crud_component_js("hero", "/heroes", ["name"], has_revisions=True)
    assert "this.hasRevisions = true;" in js
    assert "history-row" in js
    assert "/revisions?" in js


def test_render_crud_component_js_has_events_subscribes_to_event_source() -> None:
    """has_events=True subscribes to GET .../events via the browser EventSource API."""
    js = render_crud_component_js("hero", "/heroes", ["name"], has_events=True)
    assert "this.hasEvents = true;" in js
    assert "new EventSource(`${this.apiBase}/events`)" in js


def test_render_crud_component_js_stats_enabled_adds_stats_and_predict_panel() -> None:
    """stats_enabled=True renders a Stats table and a Predict control fetching /stats and
    /predict, plus an inline SVG bar chart helper -- no charting dependency.
    """
    js = render_crud_component_js("hero", "/heroes", ["name"], stats_enabled=True)
    assert "this.statsEnabled = true;" in js
    assert "renderStatsPanel" in js
    assert "${this.apiBase}/stats`" in js
    assert "${this.apiBase}/predict?" in js
    assert "<svg" in js
