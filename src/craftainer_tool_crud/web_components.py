"""Generic HTML form + web-component JS templates for any CRUD resource.

Both functions are parameterized by resource name/field list/API base path, not
tied to Hero -- see app.controllers.heroes_web for the applied example. Plain
string templates rather than a template engine (e.g. Jinja2): these pages are
small enough that a template engine would add a dependency without adding
clarity.
"""

from collections.abc import Sequence


def render_crud_form(resource: str, fields: Sequence[str], list_endpoint: str) -> str:
    """Render a zero-JS HTML page: a plain <form> that POSTs a new record, plus a table."""
    inputs = "\n".join(
        f'      <label>{field}: <input name="{field}" required></label><br>' for field in fields
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{resource} — form</title></head>
<body>
  <h1>{resource}</h1>
  <form method="post" action="{list_endpoint}/form">
{inputs}
    <button type="submit">Create</button>
  </form>
  <hr>
  <script src="{list_endpoint}/components.js"></script>
  <{resource}-list api-base="{list_endpoint}"></{resource}-list>
</body>
</html>"""


def render_crud_component_js(
    resource: str, api_base: str, fields: Sequence[str], *, list_fields: Sequence[str] = ()
) -> str:
    """Render vanilla-JS custom elements <{resource}-list>/<{resource}-form> for JSON CRUD.

    Both elements talk to the same JSON endpoints the API already serves at
    `api_base` (list/create/get/update/delete) -- no separate web-component-only
    backend, just a browser-native front end for the existing CRUD interface.
    `list_fields` names which of `fields` hold an array value: the list view joins
    it with ", " for display instead of relying on default array-to-string
    coercion, and the form splits its raw input on "," into an array before
    submitting.
    """
    fields_json = ", ".join(f'"{field}"' for field in fields)
    list_fields_json = ", ".join(f'"{field}"' for field in list_fields)
    return f"""class {resource.capitalize()}List extends HTMLElement {{
  connectedCallback() {{
    this.apiBase = this.getAttribute("api-base") || "{api_base}";
    this.refresh();
  }}

  async refresh() {{
    const response = await fetch(this.apiBase);
    const records = await response.json();
    const fields = [{fields_json}];
    const listFields = [{list_fields_json}];
    const display = (record, f) => listFields.includes(f) ? record[f].join(", ") : record[f];
    this.innerHTML = "<table><tr>" + fields.map(f => `<th>${{f}}</th>`).join("") +
      "<th></th></tr>" + records.map(record => "<tr>" +
        fields.map(f => `<td>${{display(record, f)}}</td>`).join("") +
        `<td><button data-id="${{record.id}}">Delete</button></td></tr>`).join("") +
      "</table>";
    this.querySelectorAll("button[data-id]").forEach(button => {{
      button.addEventListener("click", async () => {{
        await fetch(`${{this.apiBase}}/${{button.dataset.id}}`, {{ method: "DELETE" }});
        this.refresh();
      }});
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
