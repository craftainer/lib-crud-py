"""Generic HTML form + web-component JS templates for any CRUD resource.

Both functions are parameterized by resource name/field list/API base path, not
tied to Hero -- see app.crud_1.heroes.heroes_v2 for the applied example. Plain
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
    from a router factory call (see app.crud_1.heroes), never derived
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
    resource: str,
    api_base: str,
    fields: Sequence[str],
    *,
    list_fields: Sequence[str] = (),
    archivable: bool = False,
    draftable: bool = False,
    has_revisions: bool = False,
    has_events: bool = False,
    stats_enabled: bool = False,
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

    `archivable`/`draftable`/`has_revisions`/`has_events`/`stats_enabled` mirror
    the same-named opt-in params `app.controllers.crud_router.build_json_router`
    was given for this resource -- each gates one piece of generated UI (an
    Archive/Restore row action, a Save-as-draft/Publish pair, a per-row History
    panel fetching `GET .../revisions?id=`, a live `EventSource` subscription to
    `GET .../events` that refreshes the list, and a Stats/Predict panel) that
    calls the matching JSON route directly, the same way every other action here
    already does. Plain HTML/JS/inline SVG only -- no charting dependency, per
    docs/plans/2026-09-crud-stats-and-predictions.md's web-UI scope decision.
    """
    fields_json = ", ".join(f'"{field}"' for field in fields)
    list_fields_json = ", ".join(f'"{field}"' for field in list_fields)
    archivable_js = "true" if archivable else "false"
    draftable_js = "true" if draftable else "false"
    has_revisions_js = "true" if has_revisions else "false"
    has_events_js = "true" if has_events else "false"
    stats_enabled_js = "true" if stats_enabled else "false"
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
    this.archivable = {archivable_js};
    this.draftable = {draftable_js};
    this.hasRevisions = {has_revisions_js};
    this.hasEvents = {has_events_js};
    this.statsEnabled = {stats_enabled_js};
    this.includeArchived = false;
    this.fieldInfo = [];
    this.innerHTML =
      '<div class="filters"></div><div class="results"></div>' +
      '<div class="stats-panel"></div>';
    this.filtersEl = this.querySelector(".filters");
    this.resultsEl = this.querySelector(".results");
    this.statsEl = this.querySelector(".stats-panel");
    this.loadFilters();
    if (this.statsEnabled) this.renderStatsPanel();
    if (this.hasEvents) this.subscribeToEvents();
  }}

  subscribeToEvents() {{
    try {{
      const source = new EventSource(`${{this.apiBase}}/events`);
      source.onmessage = () => this.refresh();
    }} catch (err) {{
      // EventSource unsupported/unreachable -- the list still works via manual refresh.
    }}
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
    const archivedToggle = this.archivable
      ? '<label><input type="checkbox" class="include-archived"> include archived</label>'
      : "";
    this.filtersEl.innerHTML = `${{controls}}
      <label>Sort: <select class="sort">
        <option value="">none</option>${{sortOptions}}</select></label>
      ${{archivedToggle}}
      <button type="button" class="apply">Apply filters</button>`;
    this.filtersEl.querySelector(".apply").addEventListener("click", () => this.refresh());
    if (this.archivable) {{
      this.filtersEl.querySelector(".include-archived").addEventListener("change", (event) => {{
        this.includeArchived = event.target.checked;
        this.refresh();
      }});
    }}
  }}

  currentQuery() {{
    const params = new URLSearchParams();
    this.filtersEl.querySelectorAll("[data-field]").forEach(el => {{
      if (!el.value) return;
      params.set(`${{el.dataset.field}}__${{el.dataset.op}}`, el.value);
    }});
    const sort = this.filtersEl.querySelector(".sort").value;
    if (sort) params.set("sort", sort);
    if (this.includeArchived) params.set("include_archived", "true");
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
    const rowActions = (record) => {{
      const id = escapeHtml(record.id);
      let actions = `<button data-id="${{id}}" class="delete-row">Delete</button>`;
      if (this.archivable && record.archived_at) {{
        actions += ` <button data-id="${{id}}" class="restore-row">Restore</button>`;
      }}
      if (this.draftable && record.is_draft) {{
        actions += ` <button data-id="${{id}}" class="publish-row">Publish</button>`;
      }}
      if (this.hasRevisions) {{
        actions += ` <button data-id="${{id}}" class="history-row">History</button>`;
      }}
      return actions;
    }};
    const rows = records.map(record => "<tr>" +
      `<td><input type="checkbox" data-id="${{escapeHtml(record.id)}}"></td>` +
      fields.map(f => `<td>${{escapeHtml(display(record, f))}}</td>`).join("") +
      `<td>${{rowActions(record)}}` +
      `<div class="history-panel" data-history-for="${{escapeHtml(record.id)}}" hidden></div>` +
      "</td></tr>").join("");
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
    this.resultsEl.querySelectorAll("button.delete-row").forEach(button => {{
      button.addEventListener("click", async () => {{
        const params = new URLSearchParams({{ id: button.dataset.id }});
        await fetch(`${{this.apiBase}}?${{params}}`, {{ method: "DELETE" }});
        this.refresh();
      }});
    }});
    this.resultsEl.querySelectorAll("button.restore-row").forEach(button => {{
      button.addEventListener("click", async () => {{
        const params = new URLSearchParams({{ id: button.dataset.id }});
        await fetch(`${{this.apiBase}}/restore?${{params}}`, {{ method: "POST" }});
        this.refresh();
      }});
    }});
    this.resultsEl.querySelectorAll("button.publish-row").forEach(button => {{
      button.addEventListener("click", async () => {{
        const params = new URLSearchParams({{ id: button.dataset.id }});
        await fetch(`${{this.apiBase}}/publish?${{params}}`, {{ method: "POST" }});
        this.refresh();
      }});
    }});
    this.resultsEl.querySelectorAll("button.history-row").forEach(button => {{
      button.addEventListener("click", async () => {{
        const panel = this.resultsEl.querySelector(
          `.history-panel[data-history-for="${{button.dataset.id}}"]`
        );
        if (!panel.hidden) {{
          panel.hidden = true;
          return;
        }}
        const params = new URLSearchParams({{ id: button.dataset.id }});
        const response = await fetch(`${{this.apiBase}}/revisions?${{params}}`);
        const revisions = response.ok ? await response.json() : [];
        panel.innerHTML = "<ul>" + revisions.map(r =>
          `<li>${{escapeHtml(r.action)}} at ${{escapeHtml(r.created_at)}} ` +
          `by ${{escapeHtml(r.actor)}}</li>`
        ).join("") + "</ul>";
        panel.hidden = false;
      }});
    }});
    this.resultsEl.querySelector(".bulk-delete").addEventListener("click", async () => {{
      const ids = this.selectedIds();
      if (!ids.length) return;
      const params = new URLSearchParams({{ id__in: ids.join(",") }});
      const response = await fetch(`${{this.apiBase}}?${{params}}`, {{ method: "DELETE" }});
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
      const params = new URLSearchParams({{ id__in: ids.join(",") }});
      const response = await fetch(`${{this.apiBase}}?${{params}}`, {{
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

  async renderStatsPanel() {{
    this.statsEl.innerHTML =
      '<h3>Stats</h3><div class="stats-body">loading…</div>' +
      '<h3>Predict</h3><label>Field: <select class="predict-field"><option value="">' +
      "record count</option>" +
      this.fields
        .map(f => `<option value="${{escapeHtml(f)}}">${{escapeHtml(f)}}</option>`)
        .join("") +
      '</select></label> <label>Periods: <input type="number" class="predict-periods" ' +
      'value="4" min="1" max="52"></label> ' +
      '<button type="button" class="predict-run">Predict</button>' +
      '<div class="predict-body"></div>';
    await this.loadStats();
    this.statsEl.querySelector(".predict-run")
      .addEventListener("click", () => this.loadPrediction());
  }}

  async loadStats() {{
    const body = this.statsEl.querySelector(".stats-body");
    try {{
      const response = await fetch(`${{this.apiBase}}/stats`);
      if (!response.ok) {{
        body.textContent = "Stats unavailable.";
        return;
      }}
      const stats = await response.json();
      let html = `<p>Total: ${{stats.total}}</p>`;
      if (stats.numeric.length) {{
        html += "<table><tr><th>field</th><th>count</th><th>min</th><th>max</th>" +
          "<th>avg</th><th>sum</th></tr>" + stats.numeric.map(n =>
          `<tr><td>${{escapeHtml(n.field)}}</td><td>${{n.count}}</td>` +
          `<td>${{n.minimum ?? ""}}</td><td>${{n.maximum ?? ""}}</td>` +
          `<td>${{n.average ?? ""}}</td><td>${{n.total ?? ""}}</td></tr>`
        ).join("") + "</table>";
      }}
      if (stats.categorical.length) {{
        html += "<table><tr><th>field</th><th>value</th><th>count</th></tr>" +
          stats.categorical.map(c =>
            `<tr><td>${{escapeHtml(c.field)}}</td><td>${{escapeHtml(c.value)}}</td><td>${{c.count}}</td></tr>`
          ).join("") + "</table>";
      }}
      if (stats.time_series && stats.time_series.length) {{
        html += "<h4>Time series</h4>" + this.renderBarChart(
          stats.time_series.map(p => ({{ label: p.bucket_start, value: p.count }}))
        );
      }}
      body.innerHTML = html;
    }} catch (err) {{
      body.textContent = "Stats unavailable.";
    }}
  }}

  async loadPrediction() {{
    const body = this.statsEl.querySelector(".predict-body");
    const field = this.statsEl.querySelector(".predict-field").value;
    const periods = this.statsEl.querySelector(".predict-periods").value || "4";
    const params = new URLSearchParams({{ periods, bucket: "day" }});
    if (field) params.set("field", field);
    try {{
      const response = await fetch(`${{this.apiBase}}/predict?${{params}}`);
      if (!response.ok) {{
        const problem = await response.json().catch(() => null);
        body.textContent = problem && problem.detail
          ? `Prediction unavailable: ${{JSON.stringify(problem.detail)}}`
          : "Prediction unavailable.";
        return;
      }}
      const prediction = await response.json();
      body.innerHTML = `<p>Method: ${{escapeHtml(prediction.method)}}</p>` +
        this.renderBarChart(
          prediction.predictions.map(p => ({{ label: p.bucket_start, value: p.value }}))
        );
    }} catch (err) {{
      body.textContent = "Prediction unavailable.";
    }}
  }}

  renderBarChart(points) {{
    if (!points.length) return "<p>No data.</p>";
    const width = 300;
    const height = 80;
    const barWidth = width / points.length;
    const max = Math.max(...points.map(p => p.value), 1);
    const bars = points.map((p, i) => {{
      const barHeight = max > 0 ? (p.value / max) * (height - 10) : 0;
      const x = i * barWidth;
      const y = height - barHeight;
      return `<rect x="${{x + 1}}" y="${{y}}" width="${{barWidth - 2}}" height="${{barHeight}}" ` +
        `fill="currentColor"><title>${{escapeHtml(p.label)}}: ${{p.value}}</title></rect>`;
    }}).join("");
    return `<svg viewBox="0 0 ${{width}} ${{height}}" width="${{width}}" height="${{height}}" ` +
      `role="img" aria-label="chart">${{bars}}</svg>`;
  }}
}}

class {resource.capitalize()}Form extends HTMLElement {{
  connectedCallback() {{
    this.apiBase = this.getAttribute("api-base") || "{api_base}";
    const fields = [{fields_json}];
    const listFields = [{list_fields_json}];
    const draftable = {draftable_js};
    const draftButton = draftable
      ? '<button type="submit" data-draft="1">Save as draft</button>'
      : "";
    this.innerHTML = "<form>" + fields.map(f =>
      `<label>${{f}}: <input name="${{f}}" ${{draftable ? "" : "required"}}></label>`
    ).join("<br>") +
      `<br><button type="submit">Create</button>${{draftButton}}</form>`;
    this.querySelector("form").addEventListener("submit", async (event) => {{
      event.preventDefault();
      const isDraft = event.submitter && event.submitter.dataset.draft === "1";
      const data = Object.fromEntries(new FormData(event.target));
      listFields.forEach(f => {{
        data[f] = (data[f] || "").split(",").map(v => v.trim()).filter(v => v);
      }});
      const url = isDraft ? `${{this.apiBase}}/draft` : this.apiBase;
      await fetch(url, {{
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
