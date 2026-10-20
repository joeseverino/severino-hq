# API

The machine API is a delivery adapter beside the web UI, the CLI and MCP
([Architecture](Architecture.md)). It lets a phone, a Shortcut or a cron job run
an HQ capability over HTTP.

It adds no capability, no domain model and no business rule. Every command
comes from `hq/platform/application/capabilities.py` and every read from
`hq/platform/application/resources.py`.

```
hq/platform/api/security.py   verify a token HQ did not issue
hq/platform/api/views.py      the transport
```

## HQ verifies; it does not issue

There is no credential table in this repo, no minting UI, and no token to
rotate. Access tokens are issued by **Pocket ID**, which already owns every
other credential in the fleet, and HQ only checks them.

That asymmetry buys three things a bespoke token store would not:

| | |
|---|---|
| **Nothing to leak** | HQ stores no secret. A database dump yields no working credential. |
| **Revocation is central** | Kill the client in Pocket ID and it is dead everywhere, immediately. |
| **Real scoping** | A credential can be granted `example.write` and nothing else. |

The last one is the point. A web operator holds every capability HQ has. A
credential living on a phone should not, and here it does not: the principal's
capabilities are *exactly* the token's granted permissions, never widened.

## Configuration

| Setting | Meaning |
|---|---|
| `SEVERINO_API_RESOURCE` | The Pocket ID API resource URI. **Empty disables the surface.** |
| `SEVERINO_API_LEEWAY_SECONDS` | Clock-skew allowance: 30 seconds. |

Empty must mean off, and does: without a resource to check `aud` against, a
token minted for any *other* API on the same Pocket ID instance would verify
here on signature alone. Signature is not identity.

## Setting up a client

In Pocket ID, **Administration → APIs**:

1. Create an API. Name it, and set its **Resource** to the value you will put in
   `SEVERINO_API_RESOURCE` (e.g. `https://hq.example.com/api`). This becomes
   the `aud` claim and **cannot be changed later**.
2. Add a **Permission** for each capability the client needs, named *exactly*
   as HQ names it: `example.write`, `write_receipts`, `write_expenses`.

The permission keys are HQ's capability names on purpose. A mapping table
between the two systems would be a third home for the authorization model and
the first thing to go stale when a plugin adds a capability.

Then in **Administration → OIDC Clients**, add a client per automation, with
the client credentials grant enabled. One client per automation, not one shared
client: separate secrets rotate independently, and the token's `client_id`
becomes the actor in HQ's audit log, so an import is traceable to the
credential that caused it.

## What policy can do to a call

An operator can allow, hold, or deny each capability per client. Handle all three:

| Outcome | Response | Client should |
|---|---|---|
| Allowed | the capability's result | carry on |
| Held | `status: "awaiting_approval"`, with `approval.review_url` | report it and stop; retrying returns the same request |
| Refused | error code `denied_by_policy` | stop |

With no rules set, changes to gated infrastructure are held, and so is
anything destructive, because an API client is an agent
(`capability_policy.decide`). The rules themselves are in
[Security](Security.md#what-hq-enforces).

## Using it

Get a token:

```bash
curl -s https://sso.example.com/api/oidc/token \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" \
  -d resource="https://hq.example.com/api" \
  -d scope="example.write"
```

Ask what it may do:

```bash
curl -s https://hq.example.com/api/v2/ -H "Authorization: Bearer $TOKEN"
```

```json
{"ok":true,"data":{"actor":"example-automation","granted":["example.write"],...}}
```

Run a capability:

```bash
curl -s https://hq.example.com/api/v2/capabilities/example.import/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"records":[{"external_id":"sample-1","value":42}]}}'
```

### Routes

| Method | Path | |
|---|---|---|
| `GET` | `/api/v2/` | Who you are and what you were granted |
| `GET` | `/api/v2/openapi.json` | This API as an OpenAPI 3.2 document; also served to the signed-in operator's session, even with no API resource configured, and rendered for them at `/api/docs/` |
| `GET` | `/api/v2/capabilities/` | Every capability, flagged `permitted` for this token |
| `POST` | `/api/v2/capabilities/<name>/` | Run one |
| `GET` | `/api/v2/resources/` | Every read resource, its operations and filter schema |
| `GET` | `/api/v2/resources/<name>/` | List a resource using validated query parameters |
| `GET` | `/api/v2/resources/<name>/<identifier>/` | Get one addressable record |
| `GET` | `/api/v2/connections/` | Connection families, abilities, grant evidence, lifecycle, and safe cached state |
| `GET` | `/api/v2/topology/` | The permitted live graph, optionally narrowed by lens or a bounded dependency trace |
| `GET` | `/api/v2/findings/` | Evidence-backed claims with stable IDs, causal rollups, authorized remedies, and derived understand → act → verify workflows |

`/api/` is exempt from the session-login redirect but **not** from
authentication. An anonymous request gets `401` with a `WWW-Authenticate`
header, never a 302 to an HTML login page: a Shortcut cannot fill one in, and
would record the redirect as success while importing nothing.

### The reference

`/api/docs/` (System > API) renders the document for the signed-in operator in
a vendored viewer (`static/vendor/scalar`, pinned in `UPSTREAM`). It is a
reference, not a client:

- **Structure** is the document's own tags, nothing page-specific. Tags stand
  in the order of HQ's navigation: a group (`kind: nav`) where its first
  domain is, each domain under it by `parent`, a domain outside every group
  in its own place. The version tag is a badge (`kind: badge`) and carries
  `x-scalar-ignore`, the form the viewer reads, so it is no section.
- **Appearance** is HQ's tokens, handed to the viewer's theme variables by
  `static/css/api-reference.css`. That sheet holds no color, size or face of
  its own, and both of the viewer's modes take the same tokens, so the page
  follows the operator's theme (`data-theme` on `<html>`, else the system's)
  with no toggle of its own.
- **No request is sent from the page.** The API takes a bearer token from the
  identity provider and never the browser session, so the viewer's request
  client and its token field are off. Samples are curl, written against the
  origin serving the page; the generated CLI and TypeScript client are the way
  to call the API.
- **Search** covers the reference only and says so; ⌘K stays HQ's.
- **Nothing is fetched from, or links to, the vendor.** The policy is HQ's
  minus Trusted Types, which the viewer cannot satisfy.

`hq/platform/api/tests/test_reference.py` holds the configuration and the sheet to
the bundle (every option and variable is one the bundle reads, every sample
target it ships is hidden but curl), and `ReferenceBrowserTests` in the browser
gate holds what is drawn. After replacing the bundle, both say what moved.

### Descriptions

Each capability description includes the domain `input_schema` and the complete
HTTP `request_schema`, including target and optimistic-concurrency fields,
unknown-field rejection, and whether an idempotency key is required. Clients
can therefore generate and validate requests from the deployed composition's
actual registry; a plugin does not maintain a parallel API document.
The optional `resource` field names the `ResourceSpec` the operation acts on,
giving clients a stable way to connect discovery, reads, and available writes.
Targeted capabilities may also publish `target_label`, `target_help`,
`target_query`, `execution_notes` and `target_initial_fields`
([Plugins](Plugins.md#the-integration)); they are presentation metadata, and
`target` remains the machine identifier contract.

Resource descriptions follow the same rule. A `ResourceSpec` declares its
label, summary, required permissions, list-query model, stable identifier, and
optional search projection once. HQ derives the API catalog and read routes,
the generic MCP tools, and global-search registration from that declaration.
Unknown filters, repeated URL parameters, unregistered resources, unsupported
operations, and insufficient grants fail before a domain query runs.

### Read now

`infrastructure.controller.refresh` is the freshness loop. It marks HQ active,
rings the credential-free controller doorbell, and lets the pull-based
controller apply its usual cadence. The API process gets no provider
authority, and the caller gets the due decision behind the request.

With a subject it is "read now": `connection_ref` forces every kind that
connection's credential reads (derived from the observation registry through
its provider), `kind` forces one kind, and `every_connection` forces the whole
sweep. At most one. An unknown ref or kind is `invalid_input`; it needs
`manage_infrastructure`. The request is stored as a `ReadRequest`, audited
against its connection, and makes the controller's `sweep-due` answer due
whatever the cadence says; when only requests make it due, `only_kinds` names
every kind the controller reads. A request is answered once each forced reading
is stored after it, and stops forcing after `SEVERINO_READ_REQUEST_SECONDS`.
The web form is `POST /infrastructure/connections/read/`; a GET is refused. A
finding offers "Check again" as its verification: where a reading is involved
it reads now and runs the check on what the controller reports; otherwise it
runs the check against current data.

### What each surface exposes

Every derived read is a `ResourceSpec` in `hq/platform/application/resources.py`.
One registration serves the API (`/api/v2/resources/<name>/`), MCP
(`list_resource`, `get_resource`), the CLI (`manage.py hq_call` over those
tools) and the SDK (`hq_sdk.resources.list_resource` / `get_resource`). Each
handler calls the function its page calls. All need `read`, which every MCP
principal holds; the `SEVERINO_MCP_ENABLE_*` switches gate writes, not reads.

| Derived feature | Page | Resource |
|---|---|---|
| Estate reading (`estate.cards`) | Dashboard | `estate` |
| Action items (`dashboard.work_queue`, estate items included) | Action items | `action.items` |
| Machine catalog, roles, HQ's own machine | Machines | `machines` |
| Running containers: standing, runtime, posture, supply chain (`containers.containers`) | Containers | `containers` (`get <machine:container>`) |
| Upgrade plans, read-only (`upgrades.plans`) | Containers, container page | `upgrades` (`get <machine:container>`) |
| Domains, their services and registration | Domains | `domains` |
| Services and their facets | Services | `services` |
| Request path per hostname (`paths.path_to`) | Service page, connections | `paths` (`get <hostname>`), inside `services` get |
| How the calling request reached HQ (`request_path.request_path`) | This connection | `request.path` (the caller's own request; empty, saying why, without one) |
| `relationships_for`, with `entity_link` names | Entity pages | `relationships` (`get <node id>`) |
| Readings, schema-filtered (`hq/domains/control_plane/observations`) | Connections, entity pages | `readings` (`get <kind>`) |
| Join engine (`facts.readings`) | Entity pages | inside `relationships`, `services`, `domains` |
| Credential access | Connections | `credentials` (`get <provider>`) |
| Connections page (`connection_context.connections_context`): rows with sight, freshness, refusals, what more scope would show, credential fix, reach, last activity, pending read now; summary counts; estate posture; HQ's path | Connections | `connection.standing` (`get <connection_ref>`) |
| Tailnet page (`tailnet_context.tailnet_context`): settings, grants, shell rules, groups, tags with machines, services, app connectors, tests, findings, unread readings and why | Tailnet | `tailnet` |
| HQ itself (`hq_context.hq_context`): release and whether its branch is ahead of production, extensions composed, its machine and container, its units and credentials refresh, its scheduled work with each piece's recent runs and next due, every reading's state and age, its open problems as the items | System > HQ | `hq` |
| Estate and record search (command center) | Search | `search` |

Three projections have their own routes and MCP tools instead of resources, and
the SDK does not re-export them: topology and impact trace (`/topology/`,
`get_topology`), findings (`/findings/`, `get_findings`) and connections
(`/connections/`, `list_connections`). The registry import is the `hq.import`
capability. The public registry refresh is the scheduled `registry.refresh`
job (`manage.py refresh_public_registry --force` by hand): it runs daily and
when a sweep finds an image it has not read, and no caller triggers it.

`connection.standing` returns the estate half of the connections page. The
page also shows how the request being answered reached HQ (network admission,
transport, proxy identity and the caller's first hop); a read has no such
request, so its `request` is null. Everything else is the same object the page
renders, and a parity test holds the two equal.

Every list and detail read takes `fields`, a field mask in the AIP-161
convention: `?fields=observed_at,items.waiting` over HTTP, a list of paths to
the MCP tools and the SDK. A name keeps its whole value; a dot steps into an
object, or into every element of a list, so that example returns
`{"observed_at": …, "items": [{"waiting": …}, …]}`. Paths that share a prefix
merge, and a whole value wins over a path into it. On a list endpoint each
path is read in each item, and the collection keeps its `count`. A segment
that no record or element at its depth carries is refused with
`invalid_input`, naming the names there are; so is a path into a scalar.
Selection happens after authorization. Absent, the whole record is returned.
Because every adapter reads it beside the query, no resource query may
declare a field called `fields`; the registry refuses one at startup.

Readings leave only through their schema: `ObservationSpec.admitted` drops any
field the record model does not name. Node ids are the topology's:
`machine:<name>`, `service:<hostname>`, `zone:<domain>`, `resource:<key>`.

### Connections, topology and findings

Connection descriptions are generated by `describe_connections` from
`ConnectionSpec`. The response's `connections` array is the complete static
family catalog with a token-specific `permitted` flag; `groups` contains
runtime instances only for families the token may inspect. Runtime state
includes ability availability, granted and missing scope names, targets,
dependencies, status, and an ISO 8601 observation time. It never contains
secret material; URL userinfo, query strings, and fragments are rejected before
a controller endpoint is stored, and again at the output contract for
plugin-provided instances. Scope coverage reports availability but never
synthesizes an unregistered operation. Adding a credential changes readiness and scope state, never the shape of the
contract, and instance discovery never spends an external API call. The
connection contract itself is in
[Architecture](Architecture.md#emit-once-derive-everywhere).

The topology endpoint joins those connection observations to the deployed
ability registry and HQ's managed resources. It is a derived projection, not a
second inventory: nodes and edges disappear when their source declaration or
observation disappears. Node `actions` name the existing capability and target
behind a possible change; they do not create a topology-only mutation path.
HTTP clients execute those changes through
`POST /api/v2/capabilities/<name>/`, including the normal schema,
authorization, audit and idempotency requirements. The projection requires the
token's `read` grant and contains safe endpoint text, never credentials.

The same endpoint is also HQ's impact engine. `focus=<node-id>` selects a
bounded neighborhood; `direction=inbound|outbound|both` chooses which way to
follow declared edges, and `depth=1..5` limits traversal. The response's
`trace.hops` records every selected node's shortest distance from the focus.
Tracing composes with `lens`, costs no provider reads beyond deriving the
original authorized projection, and unknown focus values leave the projection
whole with `trace: null`. That is "what depends on this" and "what this
reaches" for generated clients, from the one graph.

Findings are another projection of that same authorized topology. General
reads collapse several stale resource kinds onto their shared controller when
the graph proves one, and expose the explained kinds in `affected_scopes`.
Clients that need the underlying machine facts can request one declared
`rule`; causal presentation never destroys the exact observations.
`severity` is a floor on the ladder `serious`, `attention`, `neutral`, `good`
(`attention` also returns `serious`), and `limit` caps how many are returned,
most severe first. `summary.findings` and `summary.severities` still count
every finding; `summary.returned` says how many came back and
`summary.truncated` whether the limit held any back. A severity off the ladder
or a limit below one is a 400 `invalid_input`. Remedies
remain references to registered capabilities, while read-only "what HQ can do
now" links come from the subject node's canonical actions rather than a second
workflow registry.

Each serialized finding may include a domain-neutral `workflow`: ordered steps
whose actions are canonical `ActionLink` contracts, plus a `claim_absent`
outcome keyed to the finding's stable claim ID. The workflow is guidance, not a
second executor; every mutation still names and enters a registered capability.

The `analytics` resource reports `coverage` for the requested completed-day
window. Coverage is recorded even when a healthy site had zero traffic, so
`missing_days` means HQ has not read that site-day, not that no visit occurred.
The controller discovers sites, asks HQ for bounded missing windows, and
backfills them idempotently; API and web readers do not invent their own
freshness policy.

### Compatibility policy

The path is the semantic major version. Additive fields may join an existing
version; removing a field, tightening accepted input, or changing retry
semantics requires a new path. Version 2 is the only contract and requires
durable idempotency for state changes. HQ has no outside clients, so a new
version replaces the old one rather than running beside it.

### Errors

Always `{"ok": false, "error": {"code", "message", "details"}}`.

| Status | Means |
|---|---|
| `400` | `invalid_input`: the request did not match its schema. Fix the fields it names. |
| `401` | No token, or it failed verification. Mint a new one. |
| `403` | Verified, but this client was not granted that capability. Fix its scope. |
| `404` | `unknown_capability`: no such capability on this deployment, or `not_found`: the record the command names does not exist. |
| `409` | `conflict`: the record changed after it was read or clashes with another, `operation_failed`: the domain refused the command, or `idempotency_conflict`: a retry key was reused with different input. |
| `413` | The request exceeds the deployment's body-size safety limit. |
| `415` | A capability request was not sent as `application/json`. |
| `503` | `SEVERINO_API_RESOURCE` is unset here. |

An `invalid_input` message names every offending field and why, in one
sentence: `project.create: name is required.`, or
`example.decide: verdict must be one of applies, does_not_apply.` It gives field
paths, expected types and declared choices, never a value the request carried.
`details` holds the structured list the sentence was built from, under the same
rule: `type`, `loc` and `msg` for a schema error, field names and error codes
for a domain one, and no submitted value in either.

### Safe retries

Every capability whose effect is not `read` requires an `Idempotency-Key`
header. Generate one opaque key per logical operation and keep it unchanged
when retrying that operation. HQ stores the actor, canonical request hash, HTTP
status, and response in the same database transaction as the domain write. A
retry therefore receives the committed response without running the command a
second time, even after a process restart. Reusing the key with different input
returns `409 idempotency_conflict`.

Records expire after 24 hours (`SEVERINO_API_IDEMPOTENCY_TTL_SECONDS`) and
expired records are pruned by the next machine write. Domain-level idempotency remains useful:
it protects imports arriving through web, CLI, or MCP, while this transport
contract protects an HTTP client that did not receive the first response.

The header is the HTTP transport's key. Every interface, MCP and CLI included,
also has one in the command itself: each capability whose effect is not `read`
accepts an optional `idempotency_key` in its payload, and its `input_schema`
says so. The rule is declared once, from the capability's effect, so no command
rejects the field and none requires it. A repeat by the same actor with the
same key and the same request returns the first result without running the
command again; the same key with a different request returns
`idempotency_conflict`. A request that is refused, or held for approval, keeps
nothing under its key, so the corrected or approved request runs. A command
that queues controller work stores the key with the operation it queues, and
generates one when the caller sent none. A command form in the browser
generates its key and keeps it hidden. A `read` capability takes no key.

## Recipe: a narrowly scoped first-party automation

This synthetic example demonstrates the transport contract without placing a
private plugin's domain vocabulary or workflow in the public host repository.
The deployed plugin's capability schema is the source of truth for its real
payload; its private repository owns the corresponding setup guide.

In an automation client:

1. Produce the source records for one logical operation.
2. Request a token from `https://sso.example.com/api/oidc/token`, POST,
   `Form` body: `grant_type=client_credentials`, `client_id`, `client_secret`,
   `resource=https://hq.example.com/api`, `scope=example.write`.
3. Read `access_token` from the result.
4. Generate a UUID and retain it as the operation's retry key.
5. POST to
   `https://hq.example.com/api/v2/capabilities/example.import/`, with
   headers `Authorization: Bearer <the value from step 3>` and
   `Idempotency-Key: <the UUID from step 4>`, `JSON` body:

   ```json
   {"payload": {"records": [{"external_id": "sample-1", "value": 42}]}}
   ```

6. Interpret the capability's typed result.

The client secret sits in the automation client. That is a real exposure and
the reason it receives only `example.write`: someone who extracts it can run
that one plugin capability, but cannot read unrelated records, touch a project,
or delete anything.

## Contract tooling

The host and controller descriptions pin OpenAPI 3.2.0, the version the
controller generator's parser (kin-openapi) recognizes. Move both to 3.2.1 when
it does; a patch release adds no features. No compatibility
translation is applied. Resource filters emit individual query parameters;
`x-hq-query-schema` retains the aggregate schema, including unknown-field policy.

Install the locked contract tools with `npm --prefix tools/clients ci`.
`mise run checks:openapi` validates both descriptions against the specification
and runs the generated client's runtime tests; `mise run drift` checks that the
client is current. The TypeScript
client and CLI are generated from the host description. Redocly marks
its client generator experimental; generated artifacts are pinned and checked
rather than assumed compatible across upgrades.

`manage.py test tests.fuzz.api_properties` generates bounded requests through Django's
real WSGI middleware using Schemathesis from the tools dependency set. Host cases
have read authority; a synthetic capability tests authorized writes and durable
retry without provider effects. Each case rolls back database changes and blocks
outbound connections. These tooling tests run explicitly in the contributor
gates; production test discovery does not require development dependencies.

All MCP tool input schemas and descriptions consume the live deployment's
OpenAPI document after Django initialization. `x-hq-mcp-tools` records MCP-only
metadata; it does not invent HTTP operations for those tools. Argument types
and defaults are emitted from the declared service signatures with the MCP
SDK and Pydantic. Resource names, identifiers and catalogs consume the
already-emitted resource paths. Registration fails if the document disagrees
with the execution signature, and calls validate against the documented
constraints before entering application code. Unknown arguments, mistyped
values and stringified JSON objects are rejected; validation errors do not
include rejected input values. Authorization and thread-sensitive execution
remain on the shared service path.

HQ serves these tools on the SDK's low-level server
(`hq/platform/mcp/server.py`): it lists the document's schemas and dispatches
to `hq/platform/mcp/binding.py`, which validates and calls the handler. Each
argument model is built from the handler's signature
(`hq/platform/mcp/declarations.py`), so nothing depends on the SDK's internals,
and an upgrade is held by the registration and validation tests. Each tool's
entry also carries its annotations: every tool but `execute_capability` is
`readOnlyHint` and closed-world (`openWorldHint: false`), which a client may use
to call reads without asking.
