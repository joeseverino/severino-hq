# Severino HQ: machine-client API

The fourth delivery adapter, after the web UI, the CLI, and MCP. It exists so a
phone, a Shortcut, or a cron job can run an HQ capability over HTTP.

It adds **no capability, no domain model, and no business rule**. Every command
comes from `hq/platform/application/capabilities.py`, and every read comes from
`hq/platform/application/resources.py`. That keeps four adapters from drifting into four
behaviours.

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
| `SEVERINO_API_LEEWAY_SECONDS` | Clock-skew allowance. Default 30. |

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

With no rules set, only changes to gated infrastructure are held.

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

Each capability description includes the domain `input_schema` and the complete
HTTP `request_schema`, including target and optimistic-concurrency fields,
unknown-field rejection, and whether an idempotency key is required. Clients
can therefore generate and validate requests from the deployed composition's
actual registry; a plugin does not maintain a parallel API document.
The optional `resource` field names the `ResourceSpec` the operation acts on,
giving clients a stable way to connect discovery, reads, and available writes.
Targeted capabilities may also publish `target_label`, `target_help`, and a
strict `target_query`. These let generated operator surfaces name the target in
domain language and derive eligible choices from the capability's registered
resource without provider I/O; `target` remains the machine identifier
contract. Optional `execution_notes` explain the registered steps an operator
is authorizing without creating a second execution plan. Optional
`target_initial_fields` declare which same-named command fields the browser may
hydrate from an authorized target detail; this is presentation metadata and
does not change the machine payload contract.

`infrastructure.controller.refresh` is the deliberate freshness loop. It marks
HQ active, rings the credential-free controller doorbell, and lets the
privileged pull-based controller apply the same cadence contract it always
uses. The web/API process receives no provider authority, and callers receive
the due decision that made the request meaningful.

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
finding whose evidence is a reading offers "Read now and check again" as its
verification; one no reading involves offers "Check again".

Each serialized finding may include a domain-neutral `workflow`: ordered steps
whose actions are canonical `ActionLink` contracts, plus a `claim_absent`
outcome keyed to the finding's stable claim ID. The workflow is guidance, not a
second executor; every mutation still names and enters a registered capability.

Resource descriptions follow the same rule. A `ResourceSpec` declares its
label, summary, required permissions, list-query model, stable identifier, and
optional search projection once. HQ derives the API catalog and read routes,
the generic MCP tools, and global-search registration from that declaration.
Unknown filters, repeated URL parameters, unregistered resources, unsupported
operations, and insufficient grants fail before a domain query runs.

### What each surface exposes

Every derived read is a `ResourceSpec` in `hq/platform/application/resources.py`. One
registration serves the API (`/api/v2/resources/<name>/`), MCP (`list_resource`,
`get_resource`), the CLI (`manage.py hq_call` over those tools) and the SDK
(`hq_sdk.resources.list_resource` / `get_resource`). Each handler calls the
function its page calls. All need `read`, which every MCP principal holds;
the `SEVERINO_MCP_ENABLE_*` switches gate writes, not these reads.

| Derived feature | Page | Resource | API | MCP | CLI | SDK |
|---|---|---|---|---|---|---|
| Estate reading (`estate.cards`) | Dashboard | `estate` | yes | yes | yes | yes |
| Action items (`dashboard.work_queue`, estate items included) | Action items | `action.items` | yes | yes | yes | yes |
| Machine catalogue, roles, HQ's own machine | Machines | `machines` | yes | yes | yes | yes |
| Running containers: standing, runtime, posture, supply chain (`containers.containers`) | Containers | `containers` (`get <machine:container>`) | yes | yes | no | no |
| Upgrade plans, read-only (`upgrades.plans`) | Containers, container page | `upgrades` (`get <machine:container>`) | yes | yes | no | no |
| Domains, their services and registration | Domains | `domains` | yes | yes | yes | yes |
| Services and their facets | Services | `services` | yes | yes | yes | yes |
| Request path per hostname (`paths.path_to`) | Service page, connections | `paths` (`get <hostname>`), inside `services` get | yes | yes | yes | yes |
| How the calling request reached HQ (`request_path.request_path`) | This connection | `request.path` (the caller's own request; empty, saying why, without one) | yes | yes | no | no |
| `relationships_for`, with `entity_link` names | Entity pages | `relationships` (`get <node id>`) | yes | yes | yes | yes |
| Readings, schema-filtered (`hq/domains/control_plane/observations`) | Connections, entity pages | `readings` (`get <kind>`) | yes | yes | yes | yes |
| Join engine (`facts.readings`) | Entity pages | inside `relationships`, `services`, `domains` | yes | yes | yes | yes |
| Credential sight | Connections | `credentials` (`get <provider>`) | yes | yes | yes | yes |
| Connections page (`connection_context.connections_context`): rows with sight, freshness, refusals, what more scope would show, credential fix, reach (network, machine, tailnet peering), last activity, pending read now; summary counts; estate posture; HQ's path | Connections | `connection.standing` (`get <connection_ref>`) | yes | yes | yes | yes |
| Tailnet page (`tailnet_context.tailnet_context`): settings, grants, shell rules, groups, tags with machines, services, app connectors, tests, findings, unread readings and why | Tailnet | `tailnet` | yes | yes | yes | yes |
| Estate and record search (command center) | Search | `search` | yes | yes | yes | yes |
| Topology and impact trace | Topology | `/topology/`, `get_topology` | yes | yes | yes | no |
| Findings | Findings | `/findings/`, `get_findings` | yes | yes | yes | no |
| Connections | Connections | `/connections/`, `list_connections` | yes | yes | yes | no |
| Registry import | none | `hq.import` capability | yes | yes | yes | yes |
| Public registry refresh | none | `manage.py refresh_public_registry` | no | no | yes | no |

`connection.standing` returns the estate half of the connections page. The
page also shows how the request being answered reached HQ (network admission,
transport, proxy identity and the caller's first hop); a read has no such
request, so its `request` is null. Everything else is the same object the page
renders, and a parity test holds the two equal.

Readings leave only through their schema: `ObservationSpec.admitted` drops any
field the record model does not name. Node ids are the topology's:
`machine:<name>`, `service:<hostname>`, `zone:<domain>`, `resource:<key>`.
Topology, findings and connections are served by their own routes and tools
rather than resources; the SDK does not re-export them. The public registry
refreshes daily and when a sweep finds an image it has not read, by design, and
is never triggered by a caller.

Connection descriptions are generated by `describe_connections` from
`ConnectionSpec`. The response's `connections` array is the complete static
family catalog with a token-specific `permitted` flag; `groups` contains
runtime instances only for families the token may inspect. Runtime state
includes ability availability, granted and missing scope names, targets,
dependencies, status, and an ISO 8601 observation time. It never contains
secret material; URL userinfo, query strings, and fragments are rejected before
a controller endpoint is stored, and again at the output contract for
plugin-provided instances. HQ's connection providers expose observations of
credentials held by their own source systems, not the credentials themselves.
An ability may name its governed resource catalog and kinds, or one exact
capability. Command Center joins those declarations to the capability catalog;
scope coverage reports availability but does not synthesize unregistered API
operations.

Host domains and extensions use the same provider contract. Each external
gateway owns its `ConnectionSpec`, and every emitted ability points to a
registered capability or resource. Consequently the connection, its reachable
records, the executable process, and that process's schema and execution notes
are joinable without adapter-specific inventories. Keyless and anonymous modes
may be emitted explicitly; adding a credential changes readiness/scope state,
not the shape of the API contract. Instance discovery remains a local read and
never spends an external API call.

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
whole with `trace: null`. This makes “show what depends on this” and “show what
this reaches” available to generated clients without creating a second graph.

Findings are another projection of that same authorized topology. General
reads collapse several stale resource kinds onto their shared controller when
the graph proves one, and expose the explained kinds in `affected_scopes`.
Clients that need the underlying machine facts can request one declared
`rule`; causal presentation never destroys the exact observations. Remedies
remain references to registered capabilities, while read-only “what HQ can do
now” links come from the subject node's canonical actions rather than a second
workflow registry.

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
| `404` | No such capability on this deployment. |
| `409` | The domain refused the command, or a retry key was reused with different input. |
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

Records expire after 24 hours by default and expired records are pruned by the
next machine write. Configure the window with
`SEVERINO_API_IDEMPOTENCY_TTL_SECONDS`. Domain-level idempotency remains useful:
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
generates one when the caller sent none. A `read` capability takes no key.

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

The host and controller descriptions use OpenAPI 3.2.0. The stable controller
generator does not yet recognize the 3.2.1 patch version correctly, so both
descriptions use the version the complete toolchain accepts. No compatibility
translation is applied. Resource filters emit individual query parameters;
`x-hq-query-schema` retains the aggregate schema, including unknown-field policy.

Install the locked contract tools with `npm --prefix scripts/openapi ci`.
`scripts/check-openapi.sh` validates both descriptions against the specification
and checks generated client freshness and runtime behavior. The TypeScript
client and CLI are generated from the host description. Redocly currently marks
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
SDK and Pydantic. Resource names, identifiers and catalogues consume the
already-emitted resource paths. Registration fails if the document disagrees
with the execution signature, and calls validate against the documented
constraints before entering application code. Unknown arguments, mistyped
values and stringified JSON objects are rejected; validation errors do not
include rejected input values. Authorization and thread-sensitive execution
remain on the shared service path.

The input-schema override uses a narrow, tested FastMCP Tool metadata seam in
`hq/platform/mcp/binding.py`; an MCP SDK upgrade must retain those registration
and validation tests.
