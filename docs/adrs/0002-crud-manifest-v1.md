# 0002. Describe CRUD resources in a versioned, discoverable manifest

## Status

Accepted

## Context

A client of a CRUD server (the Python and TypeScript SDKs, the React views,
and later the Rust server's own tests) has to know each resource's fields, the
filter operators each field takes, which optional routes exist, how big a page
may be and how long it may cache. Today that knowledge is spread across the
OpenAPI document (which has the schemas but not the filter grammar or the
capabilities), the `/filters` route and the source of
`controllers/crud_query.py`. A client that hardcodes it drifts from the server.

The server already computes most of it: `field_specs`/`describe_fields` are the
single source of truth for what the router accepts. What is missing is one
document that exposes all of it, that every implementation (Python, Rust) and
every client can test against, and that can change without breaking old clients.

## Decision

We will publish a **CRUD manifest**, `GET /api/_crud/manifest`, specified by
the JSON Schema `schemas/crud-manifest/v1.json` (draft 2020-12, `$id`
`https://craftainer.github.io/schemas/crud-manifest/v1`) and the prose in
[`docs/manifest.md`](../manifest.md).

- **Discovery** is `GET /.well-known/api-catalog` (RFC 9727,
  `application/linkset+json`): `service-desc` links the OpenAPI document and
  `describedby` links the manifest, with the schema `$id` as its `profile`. We
  do not register a new `/.well-known/` name.
- **Built from the router's own code.** The manifest is generated from the same
  `field_specs` the filter and sort parsers use, so it describes what the
  server accepts, not what someone wrote down. Where `field_specs` is too
  coarse (its `kind` lumps dates and datetimes under number) the manifest adds
  `value_type` and `choices` instead of changing the wire behavior.
- **Filtered to the caller.** A resource appears only to callers who may read
  it; an anonymous caller sees only public resources. A client never learns that
  a hidden resource exists.
- **Explicit routes.** Each optional sub-route is named in a `paths` map, present
  exactly when its capability is on, so no SDK hardcodes route suffixes.
  Resource identity is `name` + `api_version`; `v1` and `v2` of one model coexist
  and share one event stream.
- **Versioning and evolution.** `manifest_version` is `"1"`. Clients MUST ignore
  members they do not understand (the rule of OpenID Connect Discovery 1.0,
  section 3), so the schema leaves objects open (no `additionalProperties:
  false`). Adding an optional member does not bump the version; removing a
  member, making one required, or changing its meaning publishes `v2.json` under
  a new `$id` and `manifest_version`. Hints the UI will want (`default_sort`,
  field `group`/`order`, `search_fields`, relations) are expected as additive
  members.
- **Conformance fixtures** (`tests/fixtures/manifest/`, a request and an
  expected manifest per case) are the shared test data for the Python server,
  the Rust server and both SDKs.

## Consequences

- One source of truth for clients; the SDKs and the views configure themselves
  from it and need no per-resource code.
- The manifest is a public contract. Once frozen, every change is a new
  `manifest_version`, which costs a coordinated release across the server
  libraries and the SDKs.
- Open objects mean a typo in a member name is not caught by the schema; the
  fixtures and each implementation's tests carry that check.
- Every server implementation must reproduce `field_specs`' rules (and its
  quirks, for example that a field typed `IXDTFDatetime | None` is not
  filterable) to pass the fixtures; the Rust server carries that cost.
- JSON Schemas embedded for `read`/`create`/`update`/`draft` are produced by
  each implementation's own generator, so they cannot be compared byte for byte
  across languages; conformance compares them structurally (see the spec).
