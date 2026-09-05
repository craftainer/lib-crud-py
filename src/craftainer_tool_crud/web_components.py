"""Generic HTML form + web-component JS templates for any CRUD resource.

Both functions are parameterized by resource name/field list/API base path, not
tied to Hero -- see app.controllers.heroes_web for the applied example. Plain
string templates rather than a template engine (e.g. Jinja2): these pages are
small enough that a template engine would add a dependency without adding
clarity.
"""

import html
from collections.abc import Sequence


def render_crud_form(
    resource: str, fields: Sequence[str], list_endpoint: str, own_base: str
) -> str:
    """Render a zero-JS HTML page: a plain <form> that POSTs a new record, plus a table.

    `list_endpoint` (the sibling JSON API's base path) is only for the rendered
    `<{resource}-list>` web component's data calls -- the native `<form>`'s own
    `action` and the `<script src>` loading `render_crud_component_js`'s output
    must instead target `own_base`, this route's own mount path, since
    `build_resource_router` mounts JSON and web under different sub-prefixes (see
    docs/adrs/0009-...md) -- the two are no longer the same path.

    `resource`/`fields`/`list_endpoint`/`own_base` are always hardcoded values
    from a router factory call (see app.controllers.heroes), never derived
    from unsanitized request data -- html.escape here is defense-in-depth
    against a future resource that builds one of them from configuration,
    matching the escapeHtml() pattern render_crud_component_js already applies
    to record data rendered client-side.
    """
    escaped_resource = html.escape(resource)
    escaped_list_endpoint = html.escape(list_endpoint)
    escaped_own_base = html.escape(own_base)
    inputs = "\n".join(
        f"      <label>{html.escape(field)}: "
        f'<input name="{html.escape(field)}" required></label><br>'
        for field in fields
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{escaped_resource} — form</title></head>
<body>
  <h1>{escaped_resource}</h1>
  <form method="post" action="{escaped_own_base}/form">
{inputs}
    <button type="submit">Create</button>
  </form>
  <hr>
  <script src="{escaped_own_base}/components.js"></script>
  <{escaped_resource}-list api-base="{escaped_list_endpoint}"></{escaped_resource}-list>
</body>
</html>"""


def render_crud_component_js(
    resource: str, api_base: str, fields: Sequence[str], *, list_fields: Sequence[str] = ()
) -> str:
    """Render vanilla-JS custom elements <{resource}-list>/<{resource}-form> for JSON CRUD.

    Both elements talk to the same JSON endpoints the API already serves at
    `api_base` (list/create/get/update/delete/filters) -- no separate
    web-component-only backend, just a browser-native front end for the existing
    CRUD interface. `list_fields` names which of `fields` hold an array value: the
    list view joins it with ", " for display instead of relying on default
    array-to-string coercion, and the form splits its raw input on "," into an
    array before submitting.

    `<{resource}-list>` fetches `${{apiBase}}/filters` once on connect and renders
    one filter control per field it describes (a min/max pair for a numeric field,
    a text box for a string field, a `<select>` for a boolean/enum field), plus a
    sort `<select>`, building its `refresh()` query string in the same
    `field__op=value`/`sort=` wire format the server parses. A checkbox per row
    (plus a header "select all" checkbox, which selects every row currently
    listed -- i.e. every row matching the active filters) drives the bulk
    edit/delete buttons, which target exactly that selection via `id__in=` so a
    bulk action can never reach a record the visible list doesn't show.
    """
    fields_json = ", ".join(f'"{field}"' for field in fields)
    list_fields_json = ", ".join(f'"{field}"' for field in list_fields)
    return f"""function escapeHtml(value) {{
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}}

class {resource.capitalize()}List extends HTMLElement {{
  connectedCallback() {{
    this.apiBase = this.getAttribute("api-base") || "{api_base}";
    this.fields = [{fields_json}];
    this.listFields = [{list_fields_json}];
    this.fieldInfo = [];
    this.innerHTML = '<div class="filters"></div><div class="results"></div>';
    this.filtersEl = this.querySelector(".filters");
    this.resultsEl = this.querySelector(".results");
    this.loadFilters();
  }}

  async loadFilters() {{
    try {{
      const response = await fetch(`${{this.apiBase}}/filters`);
      this.fieldInfo = response.ok ? await response.json() : [];
    }} catch (err) {{
      this.fieldInfo = [];
    }}
    this.renderFilterControls();
    this.refresh();
  }}

  renderFilterControls() {{
    const controls = this.fieldInfo
      .filter(info => info.name !== "id")
      .map(info => {{
        const label = escapeHtml(info.name);
        const n = info.name;
        if (info.kind === "number") {{
          return `<label>${{label}} min: <input type="number" data-field="${{n}}"
            data-op="min"></label> <label>max: <input type="number" data-field="${{n}}"
            data-op="max"></label>`;
        }}
        if (info.kind === "boolean") {{
          return `<label>${{label}}: <select data-field="${{n}}" data-op="eq">
            <option value="">any</option><option value="true">true</option>
            <option value="false">false</option></select></label>`;
        }}
        if (info.kind === "enum") {{
          const options = (info.choices || [])
            .map(c => `<option value="${{escapeHtml(c)}}">${{escapeHtml(c)}}</option>`)
            .join("");
          return `<label>${{label}}: <select data-field="${{n}}" data-op="eq">
            <option value="">any</option>${{options}}</select></label>`;
        }}
        return `<label>${{label}}: <input type="text" data-field="${{n}}"
          data-op="icontains"></label>`;
      }})
      .join(" ");
    const sortOptions = this.fieldInfo
      .flatMap(info => [
        `<option value="${{info.name}}">${{escapeHtml(info.name)}} ascending</option>`,
        `<option value="-${{info.name}}">${{escapeHtml(info.name)}} descending</option>`,
      ])
      .join("");
    this.filtersEl.innerHTML = `${{controls}}
      <label>Sort: <select class="sort">
        <option value="">none</option>${{sortOptions}}</select></label>
      <button type="button" class="apply">Apply filters</button>`;
    this.filtersEl.querySelector(".apply").addEventListener("click", () => this.refresh());
  }}

  currentQuery() {{
    const params = new URLSearchParams();
    this.filtersEl.querySelectorAll("[data-field]").forEach(el => {{
      if (!el.value) return;
      params.set(`${{el.dataset.field}}__${{el.dataset.op}}`, el.value);
    }});
    const sort = this.filtersEl.querySelector(".sort").value;
    if (sort) params.set("sort", sort);
    return params;
  }}

  selectedIds() {{
    return [...this.resultsEl.querySelectorAll("input[type=checkbox][data-id]:checked")]
      .map(el => el.dataset.id);
  }}

  async refresh() {{
    const params = this.currentQuery();
    const response = await fetch(`${{this.apiBase}}?${{params}}`);
    const records = await response.json();
    const fields = this.fields;
    const listFields = this.listFields;
    const display = (record, f) => listFields.includes(f) ? record[f].join(", ") : record[f];
    const headCheckbox = '<th><input type="checkbox" class="select-all"></th>';
    const head = "<tr>" + headCheckbox +
      fields.map(f => `<th>${{escapeHtml(f)}}</th>`).join("") + "<th></th></tr>";
    const rows = records.map(record => "<tr>" +
      `<td><input type="checkbox" data-id="${{escapeHtml(record.id)}}"></td>` +
      fields.map(f => `<td>${{escapeHtml(display(record, f))}}</td>`).join("") +
      `<td><button data-id="${{escapeHtml(record.id)}}">Delete</button></td></tr>`).join("");
    const bulkEditInputs = fields
      .map(f => `<input class="bulk-edit-field" data-field="${{f}}"
        placeholder="${{escapeHtml(f)}}">`)
      .join(" ");
    this.resultsEl.innerHTML = `<table>${{head}}${{rows}}</table>
      <button type="button" class="bulk-delete">Delete selected</button>
      ${{bulkEditInputs}}
      <button type="button" class="bulk-edit">Update selected</button>
      <div class="bulk-result"></div>`;
    this.resultsEl.querySelector(".select-all").addEventListener("change", (event) => {{
      this.resultsEl.querySelectorAll("input[type=checkbox][data-id]").forEach(el => {{
        el.checked = event.target.checked;
      }});
    }});
    this.resultsEl.querySelectorAll("button[data-id]").forEach(button => {{
      button.addEventListener("click", async () => {{
        await fetch(`${{this.apiBase}}?id=${{button.dataset.id}}`, {{ method: "DELETE" }});
        this.refresh();
      }});
    }});
    this.resultsEl.querySelector(".bulk-delete").addEventListener("click", async () => {{
      const ids = this.selectedIds();
      if (!ids.length) return;
      const response = await fetch(
        `${{this.apiBase}}?id__in=${{ids.join(",")}}`, {{ method: "DELETE" }}
      );
      const result = await response.json();
      this.resultsEl.querySelector(".bulk-result").textContent =
        `Deleted ${{result.matched}} record(s).`;
      this.refresh();
    }});
    this.resultsEl.querySelector(".bulk-edit").addEventListener("click", async () => {{
      const ids = this.selectedIds();
      if (!ids.length) return;
      const data = {{}};
      this.resultsEl.querySelectorAll(".bulk-edit-field").forEach(el => {{
        if (el.value) data[el.dataset.field] = listFields.includes(el.dataset.field)
          ? el.value.split(",").map(v => v.trim()).filter(v => v)
          : el.value;
      }});
      const response = await fetch(`${{this.apiBase}}?id__in=${{ids.join(",")}}`, {{
        method: "PATCH",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      const result = await response.json();
      this.resultsEl.querySelector(".bulk-result").textContent =
        `Updated ${{result.matched}} record(s).`;
      this.refresh();
    }});
  }}
}}

class {resource.capitalize()}Form extends HTMLElement {{
  connectedCallback() {{
    this.apiBase = this.getAttribute("api-base") || "{api_base}";
    const fields = [{fields_json}];
    const listFields = [{list_fields_json}];
    this.innerHTML = "<form>" + fields.map(f =>
      `<label>${{f}}: <input name="${{f}}" required></label>`).join("<br>") +
      '<br><button type="submit">Create</button></form>';
    this.querySelector("form").addEventListener("submit", async (event) => {{
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.target));
      listFields.forEach(f => {{
        data[f] = data[f].split(",").map(v => v.trim()).filter(v => v);
      }});
      await fetch(this.apiBase, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(data),
      }});
      event.target.reset();
      this.dispatchEvent(new CustomEvent("created", {{ bubbles: true }}));
    }});
  }}
}}

customElements.define("{resource}-list", {resource.capitalize()}List);
customElements.define("{resource}-form", {resource.capitalize()}Form);
"""
