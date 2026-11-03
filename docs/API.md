# API

The machine API is a delivery adapter beside the web UI, the CLI and MCP
([Architecture](Architecture.md)). It lets a phone, a Shortcut or a cron job run
an HQ capability over HTTP.

It adds no capability, no domain model and no business rule. Every command
comes from `hq/platform/application/capabilities.py` and every read from
`hq/platform/application/resources.py`.

```
hq/platform/application/tokens.py   verify a token HQ did not issue, for every door
hq/platform/api/views.py            the transport
```

## HQ verifies; it does not issue

There is no credential table in this repo, no minting UI, and no token to
rotate. Access tokens are issued by **Pocket ID**, which already owns every
other credential in the fleet, and HQ only checks them:

| | |
|---|---|
| **Nothing to leak** | HQ stores no secret. A database dump yields no working credential. |
| **Revocation is central** | Revoke the client in Pocket ID and it stops working everywhere at once. |
| **Real scoping** | A credential can be granted `example.write` and nothing else. |

A web operator holds every capability HQ has. A token holds less:

| Token | Principal | Holds |
|---|---|---|
| Any client's | an agent, here as on `/api/mcp/` | the permissions the token was granted, never widened, and only those the deployment's `SEVERINO_AGENT_ENABLE_*` switches let an agent hold ([Security](Security.md#agents)) |
| A client an administrator gave the `own_device` permission | a person's own device | its granted permissions uncapped, those held for a person in the browser included; the Agents pause does not stop it |

The catalogs' `permitted` flags (capabilities, resources and connections,
here and on `/api/mcp/` alike) say what the caller may use after that cap, by
the same rule its use is refused by.

A capability held only by a person (an upload, an approval) is out of any
token's reach, whatever its grant. So neither the catalog nor the OpenAPI
document lists it for one.

## Configuration

| Setting | Meaning |
|---|---|
| `SEVERINO_API_RESOURCE` | The Pocket ID API resource URI. **Empty disables the surface.** |
| `SEVERINO_API_LEEWAY_SECONDS` | Clock-skew allowance: 30 seconds. |

Empty turns the surface off because without a resource to check `aud`
against, a token minted for any other API on the same Pocket ID instance
would verify here on its signature alone.

## Setting up a client

In Pocket ID, **Administration → APIs**:

1. Create an API. Name it, and set its **Resource** to the value you will put in
   `SEVERINO_API_RESOURCE` (e.g. `https://hq.example.com/api`). The resource
   becomes the `aud` claim and **cannot be changed later**. MCP is served at
   `/api/mcp/`, under the same path, so its resource metadata names this one
   audience too.
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

A person's own device (the phone app, a Shortcut) is a client of its own too,
signed in by that person, and its user-delegated grant on the API also holds
the `own_device` permission. Give that permission to no other client, and
never select it for the API's metadata-document clients: HQ refuses it on a
client-credentials token and on a client named by a URL.

On a client a person signs in through (their own device, or an agent with a
user-delegated grant), set **Allowed User Groups** on the **Access** tab to the
groups that may sign in. Pocket ID refuses anyone else before HQ ever sees a
token, and a client made in the admin UI admits nobody until a group is chosen.

## What policy can do to a call

An operator can allow, hold, or deny each capability per client. Handle all three:

| Outcome | Response | Client should |
|---|---|---|
| Allowed | the capability's result | carry on |
| Held | `200`, the `AwaitingApproval` the document offers beside every capability's result: `status: "awaiting_approval"`, with `approval.review_url`; over MCP, a task to a client of the tasks extension ([below](#held-changes-as-mcp-tasks)) | report it and stop, or wait on the task; retrying returns the same request |
| Refused | `403`, error code `denied_by_policy` | stop |

With no rules set, changes to gated infrastructure are held, and so is
anything destructive, because an API client is an agent
(`capability_policy.decide`). The rules themselves are in
[Security](Security.md#what-hq-enforces).

A person's own device is no agent: it stands as its person. So the `/agents/`
rules do not reach it, and nothing it asks for is held.

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

Every path, its methods and what each answers are the document's: `GET
/api/v2/openapi.json` (committed as `hq/platform/api/hq-api.openapi.json`),
rendered for the signed-in operator at `/api/docs/`.

`/api/` is exempt from the session-login redirect but **not** from
authentication. An anonymous request gets `401` with a `WWW-Authenticate`
header, never a 302 to an HTML login page: a Shortcut cannot fill one in, and
would record the redirect as success while importing nothing.

### Names

Every capability and resource name, the host's and each extension's, is
dotted snake_case:

- A capability is `<area>[.<noun>…].<verb>`: the area it belongs to, the
  singular nouns it acts on, and the verb last (`contact.submission.review`,
  `infrastructure.resource.restart`, `example.sync`). `list` reads many and
  `read` reads one or a whole (`lookup.address.read`). A plural noun names a
  set the verb acts on whole (`dashboard.panels.refresh`).
- A resource is `[<owner>.]<noun>`: plural for records read one by one
  (`projects`, `contact.submissions`), a mass or singular noun for a whole
  read at once (`tailnet`, `infrastructure`). The owner comes first only
  where the noun alone would not say whose records they are, and no resource
  is named `resources`: the path it is served at says so.

`test_registered_names.py` in `hq/platform/application/tests` holds every
registered name to these patterns, and the composed suite holds every
installed extension's. A capability's verb is one of the `VERBS` declared
there, so a new verb joins on purpose. A rename moves the rows HQ keeps under
the old name through `hq_sdk.migrations` ([Plugins](Plugins.md#renaming-a-capability)).

### The reference

`/api/docs/` (System > API) renders the document for the signed-in operator in
a vendored viewer (`static/vendor/scalar`, pinned in `UPSTREAM`). It is a
reference, not a client:

- **Structure** is the document's own tags, nothing page-specific. Each
  operation stands in the section of the domain that serves it, the host's
  core and every extension alike: the domain that declares it, else the
  domain whose nav item is its page, else, for a command, the domain of the
  resource it acts on (`_Tags` in `openapi.py`). Only the API's own routes,
  which serve every domain (the root, the catalogs, this document and the
  generic `{name}` forms), are the API domain's (`hq.api`, System > API). It
  is where a client starts, so its group leads and it leads its group. The
  rest stand in the order of HQ's navigation, each domain in the group the bar
  draws its page in (an extension may place a host page in its own group): a
  group (`kind: nav`) where its first domain is, each domain under it by `parent`, a domain outside every
  group in its own place. A domain named as its group is that group's
  section, with the others under it. A group says what it is for.
  `info.version` and the paths say the version, so no tag repeats it.
- **Appearance** is HQ's tokens, handed to the viewer's theme variables by
  `static/css/api-reference.css`. That sheet holds no color, size or face of
  its own, and both of the viewer's modes take the same tokens, so the page
  follows the operator's theme (`data-theme` on `<html>`, else the system's)
  with no toggle of its own.
- **Facts are fields.** A capability's `x-hq-effect` and
  `x-hq-required-capabilities` are drawn on its operation (`showExtensions`),
  and each refusal's codes are its `code` field's `const`, or its `enum`
  where there are several.
- **No request is sent from the page.** The API takes a bearer token from the
  identity provider and never the browser session, so the viewer's request
  client and its token field are off. Samples are curl, written against the
  origin serving the page.
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
actual registry. A plugin does not maintain a parallel API document.

The envelope (`payload`, `target`, `expected_updated_at`) is one type,
`CallEnvelope` in `hq/platform/application/call_envelope.py`, and
`request_schema` derives each capability's from it, with the command under
`payload` (required where the command has a required field) and the target
typed by its kind. `target_help` describes the target. An integer target is a
number or its digits, and `expected_updated_at` is a date and time, the
target's `updated_at` as it was read. The capability runner reads every
door's target and `expected_updated_at` with it once the caller's authority
is settled, so the API, MCP, `hq_call` and `hq_sdk` refuse one off that shape
alike, `invalid_input` with the same sentence, and a caller who may not run
the capability is refused `forbidden` whatever it sent. Before that, the API
refuses a body's own form: a field the envelope does not name, a `payload`
that is not an object, or a field sent as `null`.

A write to a record that exists (an update, or an upsert of one already
there) sets the fields its `payload` holds, and every field it leaves out
keeps its value; a new record takes each default its schema shows. An update
requires only its target: its `input_schema` requires no field the record
holds and shows no defaults, and its description says that a field left out
keeps its value. An upsert may create, so it still requires what a create
does. The update's handler declares what it keeps (`integration_specs.Kept`):
the record as a caller would send it, which `capabilities.validate_command`
lays the payload over and validates as one command, so a rule across two
fields reads the record as it will be. The runner hands a handler the names
of the fields the payload held when it declares `sent` (`capabilities._run`),
as it hands `expected_updated_at`, and `record_saves.written` is the one
place a write reads them.

The optional `resource` field names the `ResourceSpec` the operation acts on,
giving clients a stable way to connect discovery, reads, and available writes.

Targeted capabilities may also publish `target_label`, `target_help`,
`target_query`, `execution_notes` and `target_initial_fields`
([Plugins](Plugins.md#the-integration)). They are presentation metadata;
`target` remains the machine identifier contract.

Resource descriptions follow the same rule. A `ResourceSpec` declares its
label, summary, required permissions, list-query model, stable identifier, and
optional search projection once. HQ derives the API catalog and read routes,
the generic MCP tools, and global-search registration from that declaration.
Unknown filters, repeated URL parameters, unregistered resources, unsupported
operations, and insufficient grants fail before a domain query runs.

### Refresh

`infrastructure.controller.refresh` is the freshness loop. It marks HQ active,
rings the credential-free controller doorbell, and lets the pull-based
controller apply its usual cadence. The API process gets no provider
authority, and the caller gets the due decision behind the request.

With a subject it is "Refresh", and takes at most one of:

| Subject | Forces |
|---|---|
| `connection_ref` | every kind that connection's credential reads (derived from the observation registry through its provider) |
| `kind` | one kind |
| `every_connection` | the whole sweep |

An unknown ref or kind is `invalid_input`. It needs `manage_infrastructure`.

The request is stored as a `ReadRequest` and audited against its connection.
It makes the controller's `sweep-due` answer due, whatever the cadence says.
When only requests make it due, `only_kinds` names every kind the controller
reads.

A request is answered once each forced reading is stored after it, and stops
forcing after `SEVERINO_READ_REQUEST_SECONDS`. The web form is
`POST /infrastructure/connections/read/`; a GET is refused.

A finding offers "Check again" as its verification. Where a reading is
involved, it refreshes and runs the check on what the controller reports.
Otherwise it runs the check against current data.

### What each surface exposes

Every derived read is a `ResourceSpec` in `hq/platform/application/resources.py`.
One registration serves the API (`/api/v2/resources/<name>/`), MCP
(`list_resource`, `get_resource`), the CLI (`manage.py hq_call` over those
tools) and the SDK (`hq_sdk.resources.list_resource` / `get_resource`). Each
handler calls the function its page calls. All need `read`, which every MCP
principal holds; the `SEVERINO_AGENT_ENABLE_*` switches gate writes, not reads.

The reference (`/api/docs/`) lists every resource under the domain that
declares it, with what it holds and the query it takes. Topology and findings
are projections with routes and MCP tools of their own (`/topology/` and
`get_topology`, `/findings/` and `get_findings`), which the SDK does not
re-export.

The registry import is the `hq.import` capability. The public registry refresh
is the scheduled `registry.refresh` job (`manage.py refresh_public_registry
--force` by hand). It runs daily and when a sweep finds an image it has not
read, and no caller triggers it.

`connections` returns the estate half of the connections page. The
page also shows how the request being answered reached HQ (network admission,
transport, proxy identity and the caller's first hop). A read has no such
request, so its `request` is null. Everything else is the same object the page
renders, and a parity test holds the two equal.

Every list and detail read takes `fields`, a field mask in the AIP-161
convention: `?fields=observed_at,items.waiting` over HTTP, a list of paths to
the MCP tools and the SDK. Absent, a detail read returns the whole record. A
list read keeps each item to its kind's `list_fields`, declared once on its
`ResourceSpec` and named by `describe_resources` as the list's `fields`, so a
page costs what it shows. A kind that declares none keeps whole items. `*`
alone keeps every field. Selection happens after authorization.

| Path | Selects |
|---|---|
| A name | its whole value |
| A dot | a step into an object, or into every element of a list, so that example returns `{"observed_at": …, "items": [{"waiting": …}, …]}` |
| Paths that share a prefix | merged; a whole value wins over a path into it |
| On a list endpoint | each path read in each item; the collection keeps its `count` |
| A segment no record or element at its depth carries, or a path into a scalar | refused with `invalid_input`, naming the names there are |

Every adapter reads `fields` beside the query, so no resource query may declare
a field called `fields`. The registry refuses one at startup.

A list read is one page. `truncated` says more follow it, and `next` is
`{"cursor": …}`: sent back as the `cursor` filter (`?cursor=` over HTTP) with
the same filters, it reads the page after, and the last page's `next` is null.
A cursor is opaque and signed, and bound to the kind and filters it was issued
under: one sent to another list, with other filters, or altered is refused as
`invalid_input` naming `cursor`. A list cut from a queryset resumes after its
last row's key in the list's order, so a row added or removed between reads
moves no other. A list derived whole on every read (machines, containers, the
action queue) resumes at a place in it, and a row whose place moved between
reads can be skipped or shown twice. A query that offers a `cursor` is held to
it: a handler that cuts its page without the shared helpers fails the read. A
kind whose query has no `cursor` (search, which ranks each scope apart,
analytics' ranked breakdowns, and an extension's own query) answers a top
`limit`, narrowed rather than paged, with `next` null. `documentation_status`
carries the documentation list's first page and its `next`.

Readings leave only through their schema: `ObservationSpec.admitted` drops any
field the record model does not name. Node ids are the topology's:
`machine:<name>`, `service:<hostname>`, `zone:<domain>`, `resource:<key>`.

### Connections, topology and findings

Connection descriptions are generated by `describe_connections` from
`ConnectionSpec`. The contract itself is in
[Architecture](Architecture.md#emit-once-derive-everywhere).
`/connections/` (the `describe_connections` MCP tool) answers `connections`:
the complete static family catalog, with a token-specific `permitted` flag.

A connection's instances are the `connections` resource
(`/resources/connections/`, `list_resource` and `get_resource` on MCP),
only for families the token may inspect. Their state includes ability
availability, granted and missing scope names, targets, dependencies, status,
and an ISO 8601 observation time. It never contains secret material. URL userinfo, query strings, and fragments are
rejected before a controller endpoint is stored, and again at the output
contract for plugin-provided instances.

Scope coverage reports availability but never synthesizes an unregistered
operation. Adding a credential changes readiness and scope state, never the
shape of the contract. Instance discovery never spends an external API call.

The topology endpoint joins those connection observations to the deployed
ability registry and HQ's managed resources. It is a derived projection, not a
second inventory: nodes and edges disappear when their source declaration or
observation disappears. The projection requires the token's `read` grant, and
contains safe endpoint text, never credentials. Asked with no `lens` and no
`focus`, it answers the counts (`summary.kinds`) and the `lenses` that would
narrow it, with no nodes or edges; an unknown `lens` is a 400 `invalid_input`
naming every lens.

Node `actions` name the existing capability and target behind a possible
change; they do not create a topology-only mutation path. HTTP clients execute
those changes through `POST /api/v2/capabilities/<name>/`, with the normal
schema, authorization, audit and idempotency requirements.

The same endpoint is also HQ's impact engine, "what depends on this" and "what
this reaches" for any client, from the one graph:

| Parameter | Does |
|---|---|
| `focus=<node-id>` | selects a bounded neighborhood; a node the projection does not hold is a 400 `invalid_input` |
| `direction=inbound\|outbound\|both` | chooses which way to follow declared edges |
| `depth=1..5` | limits traversal; another direction or depth is a 400 `invalid_input` naming the choices |

An integer parameter here (`depth`, and the findings' `limit`) is ASCII digits,
optionally after a minus sign, and is refused otherwise with the code,
sentence and `details` MCP's `get_topology` and `get_findings` give for the
same value; each parameter
appears at most once.

The response's `trace.hops` records every selected node's shortest distance
from the focus. Tracing composes with `lens`, and costs no provider reads
beyond deriving the original authorized projection.

Findings are another projection of that same authorized topology. General
reads collapse several stale resource kinds onto their shared controller when
the graph proves one, and expose the explained kinds in `affected_scopes`.
Clients that need the underlying machine facts can request one declared
`rule`; causal presentation never destroys the exact observations.

| Parameter or member | Means |
|---|---|
| `severity` | a floor on the ladder `serious`, `attention`, `neutral`, `good` (`attention` also returns `serious`) |
| `limit` | caps how many are returned, most severe first |
| `summary.findings`, `summary.severities` | still count every finding |
| `summary.returned` | how many came back |
| `summary.truncated` | whether the limit held any back |

A severity off the ladder, a limit below one or a `rule` HQ does not declare
is a 400 `invalid_input`; the last names every rule, which the document lists as
`rule`'s enum.

Remedies remain references to registered capabilities. Read-only "what HQ can
do now" links come from the subject node's canonical actions, rather than a
second workflow registry.

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

Always `{"ok": false, "error": {"code", "message", "details", "retry_after"}}`, the last two only where they apply: `retry_after` is the whole seconds until asking again can succeed, on a refusal whose code waits (`rate_limited`, `too_many_calls`), and the `Retry-After` header repeats it. The document declares both only at that status. A refusal the caller gets past by a call or a field it did not make says which in `details`. `details.next` is the call that finds what was missing: an MCP tool and its `arguments`, which the API serves as the route of that name. That is `describe_capabilities` for an unknown capability, or `list_resource` for a record no identifier matched, beside `details.identifier`, the field its kind is addressed by. `details.field` is the argument to send (`payload.idempotency_key`, `target`). Branch on the
status; the code says which cause. A code has one status wherever it is
answered. `CODES` in `hq/platform/application/refusal_status.py` declares
each code once, with what it means, its status, the doors that can answer it
and whether it waits, and every list of codes is derived from it. Each
operation's response for a status lists the codes it answers there as its
`code` field's `const` or `enum`. A capability's operation lists only what
its declaration lets it answer: `target_required` or `target_not_allowed` by
whether it takes a target, and the retry key's and a held change's codes only
for a change. What a handler raises on its own (`not_found`, `conflict`,
`precondition`) no capability declares, so every one lists those. Operations
answering the same codes share one response, named in words: what every
endpoint answers at a status is the status (`Forbidden`), one code alone is
that code (`InvalidInput`), and any other set is the kind of call that answers
exactly it, then the status (`ReadForbidden`, `UntargetedChangeBadRequest`,
`ResourceReadNotFound`, `AnyCallNotFound` for a call by any name). Every refusal of a grant or a
policy, on any endpoint, is recorded once in the audit log as `Denied`.

An `invalid_input` message names every offending field and why, in one
sentence: `project.create: name is required.`, or
`example.decide: verdict must be one of applies, does_not_apply.` It gives field
paths, expected types and declared choices, never a value the request carried.
`details` holds the structured list the sentence was built from, under the same
rule: `type`, `loc` and `msg` for a schema error, field names and error codes
for a domain one, and no submitted value in either.

### Safe retries

Every capability whose effect is not `read` requires a retry key from every
caller but a person's browser and the host's shell (an agent, a person's own
device, an interface added later): an `Idempotency-Key` header, or
`idempotency_key` in the payload. Generate one opaque key per
logical operation, and keep it unchanged when retrying that operation. A change
sent without one is refused with `400 idempotency_key_required`, after
authority, so a client that may not run it is told that instead. A malformed
key, or a header and a payload field naming two different keys, is
`400 invalid_idempotency_key`.

HQ stores the actor, canonical request hash and response in the same database
transaction as the domain write. A retry therefore receives the committed
response, marked `Idempotency-Replayed: true`, without running the command a
second time, even after a process restart. Reusing the key with different
input returns `409 idempotency_conflict`.

Only a call that ran keeps its answer. An error, a refusal, or a call held for
approval keeps nothing under its key. So the same key runs once the rule, the
grant or the record that stopped it has changed.

Records expire after 24 hours (`SEVERINO_API_IDEMPOTENCY_TTL_SECONDS`), and
expired records are pruned by the next keyed write.

Domain-level idempotency remains useful. It protects imports arriving through
web, CLI, or MCP, while this contract protects a client that did not receive
the first response.

The key is one key, however it arrives. The header and the payload field are
two spellings of it, and every interface reads and keeps it in one place,
`run_capability` in `hq/platform/application/capabilities.py`. A key belongs to
its actor, so the same key with the same request replays whether it came by
the API or by MCP. Each capability whose effect is not `read` accepts an
optional `idempotency_key` in its payload, and its `input_schema` says so. The
field is declared once, from the capability's effect, so no command rejects it
and the schema requires it of nobody; the browser and the shell may still run
a change without one. Its shape and the header's name are declared once
(`IDEMPOTENCY_KEY_SCHEMA` in `integration_specs.py`), so the OpenAPI document's
payload field and header are one schema, `IdempotencyKey`, the shape the key is
validated by.

| Caller | Its key |
|---|---|
| A command that queues controller work | stored with the operation it queues; generated when the caller sent none |
| A command form in the browser | generated and kept hidden |
| A `read` capability | none |

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
it does; a patch release adds no features. No compatibility translation is
applied.

Resource filters emit individual query parameters; an unknown one is refused
`invalid_input`.

`mise run lint:openapi` installs the locked Redocly from `tools/lint` and
validates every description against the specification.

### The document

`hq/platform/api/openapi.py` derives the document from the API's routes and
the capability, resource and domain registries, so one added anywhere appears
in it with no edit. It says each described fragment once: a fragment several
declarations share is a named type alias where its meaning lives, written as
the component of that name with every use a `$ref`. A document holding two
identical described fragments fails `test_described_once.py`, run in
the host's suite and in the composed suite before a deploy, so an extension's
declarations are held too. Serving the document never runs it.

`mise run fix:derived` rewrites the committed `hq-api.openapi.json`, and
`mise run lint` names it when it is behind.

`manage.py test tests.fuzz.api_properties` generates bounded requests through
Django's real WSGI middleware, using Schemathesis from the tools dependency
set. Host cases have read authority. A synthetic capability tests authorized
writes and durable retry without provider effects. Each case rolls back
database changes and blocks outbound connections.

These tooling tests run explicitly in the contributor gates. Production test
discovery does not require development dependencies.

### MCP tools

Each MCP tool's input schema and description is built from its declared
handler and the resource registry, once Django has initialized, without
building the OpenAPI document.

Each tool's `inputSchema` is generated from one Pydantic model, built from the
handler's signature. A call is validated by that same model before entering
application code, and `mise run lint` holds the written document to it. A
resource tool's `name` takes only the resources the registry serves, which
are the ones the document's paths serve.
What each resource holds and the filters it takes are `describe_resources`'
answer, so the list carries the kinds as the schema's enum and, for
`get_resource`, the field each is addressed by, never the catalog itself.
With no `name`, `describe_resources` lists every kind with the `filters` its
list narrows by; with one, as `/api/v2/resources/?name=` does, that kind with
its list's whole `query_schema`. A filter that takes only some values (a
status, a dimension, a resource kind, an action item's source) declares them
as a `Literal` read from where they are declared, a model's choices or a
registry. The schema then lists them as its enum. `limit` declares the most a
page may ask for (`MAX_PAGE_SIZE`, unless its kind declares another).
A value outside them is a refusal naming the field, through either door.
`get_topology` and `get_findings` take their read's query model
(`TopologyQuery`, `FindingsQuery`) as their argument model, the one
`/api/v2/topology/` and `/api/v2/findings/` read their query string by and
the document lists as their parameters: a choice (a lens, a direction, a
rule, a severity) is a `Literal` read from where it is declared, and a bound
(`depth`, `limit`) is the model's, so both doors publish one schema and refuse
one input in one sentence.

Unknown arguments, mistyped values and stringified JSON objects are rejected.
Validation errors do not include rejected input values. Authorization and
thread-sensitive execution remain on the shared service path.

HQ serves these tools on the SDK's low-level server
(`hq/platform/mcp/server.py`). It lists those schemas and dispatches
to `hq/platform/mcp/binding.py`, which validates and calls the handler. Each
argument model is built from the handler's signature, or is the query model
its tool declares (`hq/platform/mcp/declarations.py`). So nothing depends on the SDK's
internals, and an upgrade is held by the registration and validation tests.

Each tool's entry carries a `title` for a person, declared beside its handler
(`hq/platform/mcp/services.py`) and listed as the tool's own `title`, which a
client shows before `annotations.title`, so the annotations carry the hints
alone.
Every tool but `execute_capability` and `export_year_summary` is
`readOnlyHint` and closed-world (`openWorldHint: false`), which a client may
use to call reads without asking or side by side, and a test calls each one
and holds it to writing no row. `export_year_summary` records the export in
the audit log, so it is closed-world, `idempotentHint` and not
`destructiveHint`, and never `readOnlyHint`. `execute_capability` runs any capability, so it states the
spec's worst case for a write (`destructiveHint` and `openWorldHint`), and
`idempotentHint`, because every call that changes anything carries a retry key:
one without `idempotency_key` in its payload is refused with
`idempotency_key_required`, after authority, by the same rule the API applies
to its header ([Safe retries](#safe-retries)), and a repeat with the same key
returns the first answer. A client
may then repeat a call the connection lost.

Every tool lists an `outputSchema`: an object, `anyOf` the tool's success
and the refusal envelope (`ok: false`, `error.code`, `error.message`). The
spec has every structured result meet the schema, with no exception for
`isError`, and HQ's refusals carry `structuredContent`, so the success shape
alone would be broken by every refusal. The success is the handler's return
annotation, the TypedDict the application returns
(`hq/platform/mcp/services.py`), and the API document's `data` for the same
read is that type too, so the two doors describe one answer. `ok` is the
door's own: the API wraps each answer in `Ok`, and a tool's answer is the
type alone, its refusal saying `ok: false` beside `error`. A capability's
result is its runner's answer, so `execute_capability` alone carries `ok:
true`.

Each schema is a document of its own and cannot reference another tool's:
its definitions are written out where they are used, without titles or
descriptions (`shapes.answer_schema`). A tool's shape is open, since a
client validates each answer by it as it comes and a field the answer gains
must not break the client; only a type declared with `extra="forbid"` is
closed (`get_findings`'), so a field its serializer adds fails the
output-shape tests until it is declared. A part the application writes from a
dataclass (`json_schemas.Written`, a topology node or a finding's offers) is
typed whole in the API document and left an object in the tool's shape. The
SDK's client checks a success against the schema and never a refusal, so
`test_output_shapes.py` calls every tool both ways on the showcase's data,
checks each answer with the client's own validator, holds each success to its
handler's type through Pydantic, so a required key an answer lacks fails, and
fails a field a shape names that no answer holds.

`mise run audit:mcp --base <ref>` measures what the listings cost and what
an agent's first request costs a served process. It reports the tools/list
answer's bytes and approximate tokens, each tool's part, the bytes of the
prompt, resource and resource template lists, and the server's load and first
answers. The API document and the SDK are named apart, host alone and composed.
A stage after those makes as many slow calls at once as the agent limits let
every agent make, and the same calls one after another. For each it reports the
resident and traced memory while they are in flight, and the threads and calls
at the peak.

Each list says how long it stays fresh, through the SDK's cache hints
(`CACHE_HINTS` in `hq/platform/mcp/server.py`). The lists tools/list,
prompts/list and resources/list change only with a release, so a client may
keep them for ten minutes, privately, since only a caller that signed in
reaches them. The list resources/templates/list is the caller's grants and
records, so it is stale at once.

### MCP prompts and resources

The same server offers what a person reaches through an agent's client:
prompts, which Claude Code lists as `/hq:<prompt>` commands, and resources,
which it offers in its `@` menu. Both are the SDK's own prompt and resource
handlers, over HQ's registries (`hq/platform/mcp/prompts.py`,
`hq/platform/mcp/resources.py`).

| Prompt | One message with |
|---|---|
| `needs-me` | the action queue (`action_items`), each item's status, source, label, detail, action and page |
| `status-brief` | HQ itself (`hq`), `system_health`, and the open findings at `attention` or worse, ten at most, with the summary counting every one |
| `held-requests` | every change waiting for a person, each with the sentence it is decided by and its review page |

| Resource | Reads |
|---|---|
| `hq://action_items` | the action queue, as `needs-me` reads it |
| `hq://approvals` | the held requests, as `held-requests` reads them |
| `hq://hq` | the `hq` resource's list |
| `hq://<kind>/{identifier}`, a template per kind | one record, through `get_resource` |

A URI's host is the registry's kind, so the template list is one entry for
each kind that has a detail read and that the caller may read, an extension's
kinds included as the registry holds them. The identifier is the path,
percent-encoded as a template expands it, and parsed by the kind's own
identifier type. The resource list stays the three fixed views, so its answer
does not grow with the registry.

Each template carries its kind's summary as its description. Its identifier
completes (`completion/complete`) from the kind's own listing, read as the
caller through `list_resource`. The completion holds the values that start with
what was typed, at most 100 (the spec's cap on one completion, and the largest page a
listing serves), with `hasMore` when more match or the page was cut off. A
kind the caller may not read, or one with no listing, completes to nothing,
and a prompt, which takes no argument, has nothing to complete.

Security properties:

- Each read is the application's own (`list_resource`, `get_resource`,
  `findings`, `approvals.pending`), as the caller and under its
  authorization; nothing is read at a ceiling of its own. A caller without
  `read` is refused every prompt and fixed view, and a kind it may not read is
  neither listed as a template nor read.
- The brake on agents applies to `prompts/get` and `resources/read` as it does
  to a tool call. Outside a tool call no result carries a refusal, so it is a
  protocol error: `-32602` for invalid input or a record not found, `-32603`
  for a failure, whose text stays in the log, and `-32600` otherwise, each
  with HQ's code in `data.code` (`forbidden`, `agents_paused`). The lists are
  not braked, as the tool list is not, so a paused agent keeps its connection.
- The held requests are the caller's own, or every one for a person, as the
  action queue carries them to that caller, with the request's fields and not
  its payload.

### Held changes as MCP tasks

A client that declares the `io.modelcontextprotocol/tasks` extension on a
request (`_meta["io.modelcontextprotocol/clientCapabilities"].extensions`, on
the 2026-07-28 wire) and makes a call HQ holds is answered with a task in
place of the held answer: `resultType: "task"`, whose `taskId` is the hold's
`approval.id`. The SDK ships no tasks implementation, so HQ serves the
extension's methods over its holds (`hq/platform/mcp/tasks.py`) through the
low-level server's `add_request_handler`, and advertises the extension in its
capabilities. A client that declares nothing keeps the held answer.

| Hold | `tasks/get` answers |
|---|---|
| Waiting for a person | `working`, with the review page in `statusMessage` |
| Approved | `completed`, `result` the call's answer as it ran |
| Declined | `completed`, `result` a tool error coded `declined`, with the person's note |
| Lapsed unanswered, or overtaken by a change to what it acts on | `completed`, `result` a tool error coded `expired` or `stale` |

A decline, a lapse and an overtaken hold are tool errors, not `failed`. The
extension's specification keeps `failed` for "a JSON-RPC error during
execution" and says it "MUST NOT be used for non-JSON-RPC errors"
(tasks.extensions.modelcontextprotocol.io, specification, `Task` status). HQ
answers every other refusal as a tool error.

`pollIntervalMs` is 30,000, the longest interval Claude Code honors: a person
answers in minutes or hours, so a sooner poll only reads the same hold again.
`ttlMs` is the hold's own window, from its request to its expiry.

Security properties:

- The task is the hold, read afresh at each `tasks/get`; no task state is
  kept apart from it. Its id is the hold's random UUID.
- Nothing in the extension decides a hold. A hold asks for no input, so
  `tasks/update` acknowledges and changes nothing, and `tasks/cancel` is
  refused: the change stays a person's to approve or decline in HQ, under the
  rules every approval keeps.
- A task answers only the actor and interface that asked. Every task method
  refuses any other id with the same `-32602`, so an agent cannot tell
  another's task from an id nothing holds. A call that repeats what another
  agent already asked is answered with that agent's request, so it is given
  the held answer and no task.
- The task methods are under the brake as `prompts/get` is, and answer
  `-32021` to a request that did not declare the extension.
