# CRUD manifest, version 1

A CRUD server describes its resources in one JSON document so a client can
configure itself: which resources exist, the shape of their records, which
filters and sorts the server accepts, which optional routes are on, and how
responses may be cached. This page is the prose spec. The machine-readable
contract is [`schemas/crud-manifest/v1.json`](../schemas/crud-manifest/v1.json)
(JSON Schema draft 2020-12, `$id`
`https://craftainer.github.io/schemas/crud-manifest/v1`); the decision and its
reasons are in [ADR 0002](adrs/0002-crud-manifest-v1.md). Where this page and
the schema disagree, the schema wins.

## Discovery

```
GET /.well-known/api-catalog          application/linkset+json (RFC 9727)
GET /api/_crud/manifest               application/json
```

```json
{ "linkset": [ { "anchor": "https://host/api",
    "service-desc": [ { "href": "/openapi.json", "type": "application/vnd.oai.openapi+json" } ],
    "describedby":  [ { "href": "/api/_crud/manifest", "type": "application/json",
                        "profile": "https://craftainer.github.io/schemas/crud-manifest/v1" } ] } ] }
```

A client fetches the catalog, picks the `describedby` link whose `profile` it
understands, and fetches the manifest. The catalog needs no authentication.

`service-desc` is optional in RFC 9727 and is advertised only when the app serves an
OpenAPI document: `api_catalog_router` takes the serving app's `openapi_url` by default
(`None` omits the link, a custom path is used as is). Its `openapi_url` argument
overrides that with a string, or omits the link with `None`.

The manifest requires the same roles as reading each resource it lists. It is
**filtered to the caller**: a resource the caller cannot read is omitted
entirely (not marked, not refused), so a client never learns a hidden resource
exists. An unauthenticated caller sees only public resources; an authenticated
caller whose roles match nothing is treated the same way. The response is
`200` with `ETag` and `Cache-Control: private, no-cache`; a matching
`If-None-Match` gets a bodiless `304`. `resources` is ordered by `name`, then
by `api_version`.

## Top level

| Property | Type | Meaning |
| --- | --- | --- |
| `manifest_version` | `"1"` | Contract version. |
| `service` | `{name, version}` | The serving application. |
| `resources` | array | One entry per readable resource. Identity is `name` + `api_version`; v1 and v2 of one model are two entries. |

## Resource

| Property | Type | Meaning |
| --- | --- | --- |
| `name`, `title` | string | Machine name (`heroes`) and display name. |
| `api_version` | `v<N>` | The resource version, as in the route. |
| `base_path` | path | Collection URL of the JSON API (the router's `/json` mount). Record routes hang off it: `GET base_path` lists, `?id=` addresses one record. |
| `formats` | object | Format to root path: `json` (always, equal to `base_path`), and `xml`/`web` when mounted. Only `json` is described further here. |
| `id_field` | string | Name of the identifier property in the read schema. |
| `schemas` | object | `read`, `create`, `update`: JSON Schemas, self-contained (internal `$defs`/`$ref`). `draft` is the schema a `/draft` body validates against, or `null`. |
| `fields` | array | The **filterable and sortable** fields (see below). A record field that is not filterable (a list, a nullable `IXDTFDatetime`) appears only in `schemas.read`. |
| `pagination` | object | `style: "offset"`, `skip_param`, `limit_param`, `sort_param`, `default_limit`, `max_limit`, `total`. List responses are bare arrays; `total` is `false` (no count is sent). A `limit` over `max_limit` is a validation error. |
| `capabilities` | object | Booleans for what the resource supports; `events` is an object or `null` (below). |
| `paths` | object | Absolute path of each sub-route. `filters` and `clone` are always present; `stats`, `predict`, `revisions`, `draft`, `publish`, `restore`, `events` are present **exactly when** their capability is on. A client uses these and never builds a suffix itself. |
| `bulk` | object | `max_matched`: a bulk `PATCH`/`DELETE`/restore whose filters match more records is refused with `400`. |
| `auth` | object | `scheme: "bearer"`, `oidc_discovery_url` (or `null`), `public_read`, and the role lists `read_roles`, `write_roles`, `delete_roles` (a caller needs any one). When `public_read` is true, `read_roles` is empty. The lists describe the resource; the caller-filtering above is what hides it. |
| `cache` | object | Caching hints (below). |

### Fields

| Property | Meaning |
| --- | --- |
| `name` | Property name; also the query key. |
| `value_type` | `integer`, `number`, `date`, `datetime`, `string`, `boolean` or `enum`. The server's own `kind` lumps dates and datetimes under number; `value_type` separates them. |
| `filter_ops` | Which operators the server accepts for the field. |
| `sortable` | Whether `sort=` accepts it. Today this always equals "is in `fields`", because `parse_sort` uses the same `field_specs`; it is a separate flag so that can change without a new version. |
| `choices` | The allowed values for `enum` fields (strings, in declaration order); otherwise `null`. |
| `numeric` | Plain integer or number: eligible for numeric `/stats` aggregates and `/predict?field=`. |
| `categorical` | Boolean or enum: eligible for a `/stats` value distribution. |
| `read_only` | In neither `schemas.create` nor `schemas.update`. |
| `required` | Required by `schemas.create`. |

Filter query grammar (from `crud_query`): `field=v` is equality; every other
operator is `field__<op>=v`. `min` and `max` are **inclusive** range bounds
(`>=`, `<=`); `in` takes a comma-separated list; `contains`, `icontains` and
`regex` are string operators (a regex longer than 200 characters is rejected).
Operators by `value_type`: `integer`/`number`/`date`/`datetime` take
`eq min max in`; `string` takes `eq contains icontains regex`; `boolean` and
`enum` take `eq in`. Datetimes are sent as ISO 8601; a timezone-aware value is
converted to UTC. `sort=a,-b` is comma-separated, a leading `-` meaning
descending. Reserved query keys are `skip`, `limit`, `sort`, `id`,
`include_archived` and `include_unpublished`. An unknown field, an operator the
field does not take, or an unparsable value is a client error, never ignored.
For a float field (`number`) `nan` and `inf` are unparsable: `422`.

**`?id=` takes no filters.** `GET`, `PATCH` and `DELETE` with `?id=` together with
any non-reserved query key (a filter, recognised or not) is `422` (`loc`
`["query", <key>]`, `filters cannot be combined with id`); nothing is read or
written. Address one record by `id`, or many by filters, never both. The
reserved keys may accompany `?id=`.

### Capabilities

`bulk_update`, `bulk_delete`, `clone`, `draft_publish`, `archive_restore`,
`revisions`, `stats`, `predict`, `conditional_requests`, `owner_scoped` and
`schedulable` are booleans. `owner_scoped` means callers see only records they
own. `schedulable` means the model is Schedulable: it has `publish_at`/
`unpublish_at`, and list and get routes hide records outside their publication
window unless the request passes `?include_unpublished=true` (a reserved query
key; a model that is not schedulable ignores it). `clone` and
`conditional_requests` are on for every resource the Python server builds today;
they are flags so another implementation can differ.

`events` is `null` (no event stream) or:

| Property | Meaning |
| --- | --- |
| `transport` | `"sse"` (`text/event-stream`). |
| `path` | The stream; equals `paths.events`. |
| `resource` | The label inside each event's `resource` member. It carries **no version**: v1 and v2 of one model publish to, and share, one stream, so a client filters by `snapshot` shape or ignores the difference. |
| `resume_param` | Query parameter that resumes a subscription (`subscriber_id`); the `Last-Event-ID` header is accepted as a fallback. |
| `replay` | `persistent`: events missed while disconnected are replayed on resume (MQTT-backed). `none`: a reconnect starts fresh (in-memory). |
| `keepalive_seconds` | Idle seconds before a `: keep-alive` comment frame. |
| `actions` | Actions the stream can carry: a subset of `create`, `update`, `delete`, `restore` (bulk actions emit one event per record). |

Stream frames: the first is `id: 0` with `data: {"subscriber_id": "..."}` (store
it and pass it back as `resume_param`). Each later frame has an incrementing
`id` and `data` of `{"resource", "record_id", "action", "snapshot", "timestamp"}`.
The `id` numbers are informational; they do not resume a gap.

Subscriber ids are minted by the server and are opaque to clients: 64 hex
characters, 128 random bits plus a 128-bit HMAC-SHA256 tag over the resource and
those bits (`HMAC-SHA256(secret, resource + 0x00 + nonce_hex)[:16]`), keyed by
`CrudConfig.subscriber_secret` (at least 32 bytes; random per process when
unset). A resume id the server did not mint for this resource (guessed, forged,
from another resource, or minted under another secret, e.g. before a restart
with a random secret) is ignored: the stream starts a fresh session under a new
id, which the first frame carries. A client always stores the id from the first
frame.

### Forecast (`paths.predict`)

An ordinary-least-squares line over the bucketed series, regressed on bucket
**time**: `x_i` is the whole number of buckets between the first bucket's start
and bucket `i`'s start (`day`: days; `week`: days / 7, floor; `month`:
`12 * (year_i - year_0) + (month_i - month_0)`), so an empty bucket leaves a gap
in `x` instead of shifting later points. With `n` points, `y_i` the bucket value
and all sums accumulated left to right in IEEE-754 doubles (no compensated or
pairwise summation):

```
mean_x = (x_0 + ... + x_{n-1}) / n          mean_y = (y_0 + ... + y_{n-1}) / n
sxx = sum (x_i - mean_x) * (x_i - mean_x)   sxy = sum (x_i - mean_x) * (y_i - mean_y)
slope = 0.0 if sxx == 0 else sxy / sxx      intercept = mean_y - slope * mean_x
value_k = slope * (x_{n-1} + k) + intercept            for k = 1..periods
```

Projected bucket `k` starts `k` whole buckets after the last bucket. Fewer than
2 buckets of history is `422`.

### Cache

| Property | Meaning |
| --- | --- |
| `etag` | Responses carry an `ETag`. |
| `max_age` | Seconds a response is fresh without revalidation (`0` today). |
| `stats_ttl` | Server-side TTL of cached `/stats` and `/predict` bodies; `null` when no stats cache is configured or its TTL is not known. Such responses carry `X-Stats-Cache: hit|miss`. |
| `stale_while_revalidate` | Seconds a stale response may be served while revalidating (`0` today). |
| `list_etag` / `record_etag` | `weak` for a page of records, `strong` for a single record; `null` when `etag` is false. |
| `revalidate_only` | The server sends `Cache-Control: private, no-cache`: a client may store a response but must revalidate it before every reuse. Nothing is shared-cacheable. |

A client never caches a response that carries `Cache-Control: no-store`.

## Operations and status codes

These are the routes `paths` and `base_path` point at, as the Python server
implements them. `detail` in an error body is a string, or, for request
validation, a list of validation errors (`loc`, `msg`, `type`).

| Operation | Success | Errors |
| --- | --- | --- |
| `GET base_path` (list, or one record with `?id=`) | `200`; `304` on a matching `If-None-Match` | `400`/`422` unrecognized filter or sort, bad value (including `nan`/`inf`), bad limit, `?id=` with filters; `401`; `403`; `404` unknown `?id=` |
| `POST base_path` | `201`, the record | `401`; `403`; `422` body invalid |
| `PATCH base_path?id=` | `200`, the record, new `ETag` header | `404`; `412` stale `If-Match`; `423` record locked; `422` (including `?id=` with filters) |
| `PATCH base_path?<filters>` (bulk) | `200` `{matched, ids}` | `400` no filters, or more than `bulk.max_matched` matches; `423` |
| `DELETE base_path?id=` | `204` | `404`; `412`; `423`; `422` `?id=` with filters |
| `DELETE base_path?<filters>` (bulk) | `200` `{matched, ids}` | as bulk `PATCH` |
| `POST paths.clone?id=` | `201`, the copy | `404` |
| `POST paths.draft` | `201`, the draft | `422` |
| `POST paths.publish?id=` | `200`, the record | `404`; `422` still missing required fields |
| `POST paths.restore` (`?id=` or filters) | `200` | `404`; `400` as bulk |
| `GET paths.revisions?id=` | `200`, newest first | `404`/empty |
| `GET paths.stats`, `paths.predict` | `200` | `422` bad `bucket`, `field` or `periods` |
| `GET paths.filters` | `200`, the same information as `fields` (`kind` instead of `value_type`) | |
| `GET paths.events` | `200` `text/event-stream` | |

**Conditional requests.** `ETag` on a single record is strong (id plus
`updated_at`); on a list it is weak. `If-None-Match` uses weak comparison.
`If-Match` applies to **single-record (`?id=`) `PATCH` and `DELETE` only**: a
bulk action has no one representation to compare, so `If-Match` is ignored
there. It uses strong comparison; `*` matches any existing record; a stale tag
is `412`.

**CORS.** A browser client can read `ETag` (and `X-Stats-Cache`) only if the
server lists them in `Access-Control-Expose-Headers`, and can send `If-Match`/
`If-None-Match`/`Authorization` only if they are in `Access-Control-Allow-Headers`.
A server that serves the manifest cross-origin must configure both.

## Versioning and evolution

`manifest_version` is `"1"` and the schema `$id` ends in `/v1`. The rule is the
one OpenID Connect Discovery 1.0 (section 3) uses: **clients MUST ignore members
they do not understand**, so the schema does not set `additionalProperties:
false` on any object a server may extend. Therefore:

- Adding an optional member (a property, a `paths` key, a `capabilities` flag, a
  field attribute) is **not** a version bump.
- Removing a member, making an optional member required, or changing a member's
  meaning or allowed values is a **new `manifest_version`** with a new schema
  file and `$id`; v1 is never edited in an incompatible way once frozen.
- A producer must send every member v1 lists as required; a consumer must not
  fail on a member it does not know.

Hints expected later as additive members (not part of v1 today): a resource's
`default_sort`, `search_fields` and `relations`, and per-field display hints such
as `group`, `order` and role-specific visibility.

A server may serve several versions (the catalog then lists one `describedby`
link per `profile`). A client checks `manifest_version` first and refuses a
document whose version it does not know. The discovery linkset is extensible the
same way (RFC 9727 builds on RFC 9264 linksets, which allow extra members).

## Conformance fixtures

`tests/fixtures/manifest/` holds the shared test data. `server.json` is the setup
(resources and the roles gating them). Each case `<name>` is a pair:

- `<name>.request.json`: `method`, `path`, `authenticated`, `roles`.
- `<name>.manifest.json`: the manifest that request must return.

Cases: `anonymous` and `stranger` (public only), `hero-reader` (one resource plus
public), `legacy-reader` (the other version), `admin` (everything: `heroes` v1
and v2 side by side, v2 with every capability on, `quests` with every optional
capability off, an enum field and a date field). The hero schemas are generated
from the Heroes views in `tests/support/`; `quests` and `heroes` v1 are
fixture-only.

A server implementation passes a case when its response validates against the
schema and equals the fixture, except that `schemas.read`, `schemas.create`,
`schemas.update` and `schemas.draft` are compared structurally rather than byte
for byte: the same `properties` names, the same `required` set, and the same
`type` of each property (other keywords, titles and descriptions are generator
detail and not compared). Everything else is compared exactly. An SDK passes by
loading every fixture and exposing the same resources, fields, operators, paths
and cache hints.
