# API

The machine API is a delivery adapter beside the web UI, the CLI and MCP
([Architecture](Architecture.md)). It lets a phone, a Shortcut or a cron job run
an HQ capability over HTTP.

It adds no capability, no domain model and no business rule. Every command
comes from `hq/platform/application/capabilities.py` and every read from
`hq/platform/application/resources.py`.

To call it, read [Setting up a client](#setting-up-a-client), then
[Using it](#using-it). [Contract tooling](#contract-tooling) is for changing
it, and its [MCP tools](#mcp-tools) section for the door agents use.

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
| Any client's | an agent, here as on `/api/mcp/` | the permissions the token was granted, never widened, within the role of the person who signed it in, if one did ([Security](Security.md#agents)) |
| A client an administrator gave the `own_device` permission | a person's own device | its granted permissions uncapped, those held for a person in the browser included; the Agents pause does not stop it |

An agent's call from a tailnet device does nothing until the person it acts for
approves that device for its client on the Agents page. Until then it is a 403
`device_awaiting_approval`. From a device a person blocked it is a 403
`device_blocked`, while the client's other devices run. Where the deployment
has a tailnet, a call no tailnet device sent (from loopback, or an address no
peer holds) is a 403 `device_unknown`, and one tailscaled could not answer for
a 503 `tailnet_unavailable` with `retry_after`. Only a deployment with no
tailnet keeps the client's own rules for every call, and a person's own device
needs no approval ([Security](Security.md#agents)).

The catalogs' `permitted` flags (capabilities, resources and connections,
here and on `/api/mcp/` alike) say what the caller may use after that cap, by
the same rule its use is refused by.

A capability held only by a person (an upload, an approval, adopting
playbooks) is out of any token's reach, whatever its grant. So neither the
catalog nor the OpenAPI
document lists it for one. A person's own settings, pins and set-asides are a
token's only where a person signed it in, so the catalog lists them to that
token and never to a machine's own.

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
2. Write the API's permissions from HQ's catalog, on your own machine, with a
   Pocket ID admin's API key (**Administration → API Keys**):

   ```sh
   POCKET_ID_API_KEY='op://Example Vault/pocket-id/credential' \
     op run -- mise run wire-pocket-id --pocket-id https://id.example.com \
     --resource https://hq.example.com/api --grant example-client
   ```

   The task derives the list from the extension set `mise.local.toml` names,
   as production composes it: every capability a token could carry, and
   `own_device`. It replaces the API's whole list with it and names any
   permission it removed. `--grant` gives that client every permission but
   `own_device`, delegated by a signed-in person, and keeps the rest of its
   grant. Without the key it prints the list and changes nothing. Run it again
   at the deploy of any release that adds a capability: Pocket ID refuses a
   sign-in that asks for a permission it does not list.

For a machine client that needs only selected permissions already registered on
this API, use `--machine-grant` with `--permissions` instead of `--grant`:

```sh
POCKET_ID_API_KEY='op://Example Vault/pocket-id/credential' \
  op run -- mise run wire-pocket-id --pocket-id https://id.example.com \
  --resource https://hq.example.com/api --machine-grant example-client --permissions read
```

This replaces only that client's machine grant with the exact comma-separated
permission names. It leaves the API catalog and the client's delegated grant
unchanged, refuses unknown or duplicate names and `own_device`, and refuses
reusing a client registered as a person's own device. Without the admin key it
changes nothing.

The permission keys are HQ's capability names on purpose. A mapping table
between the two systems would be a third home for the authorization model, and
the first to go stale when an extension adds a capability. So the list is
written from the catalog, never typed. HQ keeps no admin key: Pocket ID's admin
keys carry no scopes, so one could do anything.

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

On a person's own device's client, set **Allowed User Groups** on the
**Access** tab to the groups that may sign in. Pocket ID refuses anyone else
before HQ ever sees a token, and a client made in the admin UI admits nobody
until a group is chosen.

The steps for an agent's client, one a person signs in or one that runs
unattended, are on HQ's Agents page, under **Setup**
(`/agents/#setup`). Its permissions step links back here for the
task above. The page also names, on each agent's row, a permission HQ offers
that its last sign-in did not carry, so one a release adds is granted before
a sign-in asks for it.

## What policy can do to a call

An operator can allow, hold, or deny each capability per client. Handle all three:

| Outcome | Response | Client should |
|---|---|---|
| Allowed | `200`, the capability's result | carry on |
| Held | `202 Accepted`, since nothing has happened yet: the `AwaitingApproval` the document lists under `202` on every capability that changes something, `status: "awaiting_approval"`, with `approval.review_url`; over MCP, a task to a client of the tasks extension ([below](#held-changes-as-mcp-tasks)) | report it, then read `approvals` by its id or wait on the task; retrying returns the same request, at `202` |
| Refused | `403`, error code `denied_by_policy` | stop |

With no rules set, changes to gated infrastructure are held, and so is
anything destructive, because an API client is an agent
(`capability_policy.decide`). A procedure run that changes a machine is held
whatever the rules say, since no rule may allow it. The rules themselves are in
[Security](Security.md#what-hq-enforces).

A person's own device is no agent: it stands as its person. So the `/agents/`
rules do not reach it, and nothing it asks for is held.

### Finding a playbook, reading a hold and a playbook's run

Every adopted playbook lists at `GET /v2/resources/playbooks/`: its id, the
command that runs it (`automation.<id>.run`), its effect, and in `reachable`
the machines a run of it may name now, the ones HQ's runner has a login to as
the playbook needs. A read of one adds what its Run page states of it. The
newest scan of the repository reads at `/v2/resources/playbook_scans/`: what
HQ refuses in it, what adopting it widens, and whether it waits for a person,
who adopts or rejects it in HQ; a token is refused both.

A held call's `approval.id` reads where it stands at
`GET /v2/resources/approvals/<id>/` (MCP: `get_resource` of `approvals`, or
`hq://approvals/{id}`): `done`, `said`, the `operation` its approved call
queued, and once done the call's `answer`, as a task's `result` reads it
([below](#held-changes-as-mcp-tasks)). A playbook's run reads at
`GET /v2/resources/runs/<operation_id>/` by the `operation.id` its answer
carried, and lists at `/v2/resources/runs/` by `playbook`, `machine`,
`correlation` and `state`.

- Each is a read under the caller's own `read` grant.
- A hold is its asker's alone (`approvals.asked_by`, as a task is): another
  caller's id answers `not_found`, as an id nothing holds does.
- A run shows what Jobs shows of it: its machines, who asked and approved it,
  its worker while one runs it, and each task's host, name, outcome and
  message (`run_reads`), never the rest of the worker's report.

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
the old name through `hq_sdk.migrations` ([Extensions](Plugins.md#renaming-a-capability)).

### The reference

`/api/docs/` (System > API) renders the document for the signed-in operator in
a vendored viewer (`static/vendor/scalar`, pinned in `UPSTREAM`). It is a
reference, not a client:

- **Structure** is the document's own tags, nothing page-specific. Each
  operation stands in the section of the domain that serves it, the host's core
  and every extension alike (`_Tags` in `openapi.py`). That is the domain that
  declares it, else the domain whose nav item is its page, else, for a command,
  the domain of the resource it acts on. Only the API's own routes, which serve
  every domain (the root, the catalogs, this document and the generic `{name}`
  forms), are the API domain's (`hq.api`, System > API). It is where a client
  starts, so its group leads and it leads its group. The rest stand in the
  order of HQ's navigation, each domain in the group the bar draws its page in,
  which an extension may set for a host page. A group (`kind: nav`) stands
  where its first domain is, each domain under it by `parent`, and a domain
  outside every group in its own place. A domain named as its group is that
  group's section, with the others under it. A group says what it is for.
  `info.version` and the paths say the version, so no tag repeats it.
- **Appearance** is HQ's tokens, handed to the viewer's theme variables by
  `static/css/api-reference.css`. That sheet holds no color, size or face of
  its own, and both of the viewer's modes take the same tokens, so the page
  follows the operator's theme (`data-theme` on `<html>`, else the system's)
  with no toggle of its own. No rule of HQ's own stylesheet reaches the
  markup the viewer draws: its mount carries `data-vendored`, where each of
  HQ's layers gives way (`tokens.css`), and `ReferenceBrowserTests` reads
  every element it draws for a property an HQ rule gave it.
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
actual registry. An extension keeps no API document of its own.

The envelope (`payload`, `target`, `expected_updated_at`) is one type,
`CallEnvelope` in `hq/platform/application/call_envelope.py`, and
`request_schema` derives each capability's from it, with the command under
`payload` (required where the command has a required field) and the target
typed by its kind. `target_help` describes the target. An integer target is a
number or its digits, and `expected_updated_at` is a date and time, the
target's `updated_at` as it was read. The capability runner reads every door's
target and `expected_updated_at` with it once the caller's authority is
settled. So the API, MCP, `hq_call` and `hq_sdk` refuse one off that shape
alike, `invalid_input` with the same sentence. A caller who may not run the
capability is refused `forbidden` whatever it sent. Before that, the API
refuses a body's own form: a field the envelope does not name, a `payload` that
is not an object, or a field sent as `null`.

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
([Extensions](Plugins.md#the-integration)). They are presentation metadata;
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
handler calls the function its page calls. All need `read`, which a token
grants like any other permission.

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

A masked answer validates against the OpenAPI document. A document cannot say
an answer's shape follows a query parameter. So each record and row schema, and
every part a mask can reach inside one at any depth, lists no `required` key.
Each field it names keeps its type and stays closed to keys it does not name. A
part shared with another answer reads the same there. The test client validates
every answer it receives, masked ones included.

A list read is one page. `truncated` says more follow it, and `next` is
`{"cursor": …}`: sent back as `cursor` (`?cursor=` over HTTP, and an argument
of `list_resource` beside its `filters` and `limit`, as `get_findings` and
`recent_activity` take `limit`) with the same filters, it reads the page
after, and the last page's `next` is null. On MCP, a `limit` or `cursor` sent
inside `filters` is refused, so paging has one place on every tool.
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
(`/resources/connections/`, `list_resource` and `get_resource` on MCP), only
for families the token may inspect. Their state includes ability availability,
granted and missing scope names, targets, dependencies, status, and an ISO 8601
observation time. It never contains secret material. URL userinfo, query
strings, and fragments are rejected before a controller endpoint is stored, and
again at the output contract for instances an extension provides.

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

MCP's `get_findings` answers the same findings in a shape of its own
(`hq/platform/mcp/finding_actions.py`, `schema_version` 3), since an agent
cannot follow a web path. Every action is a tool call, `{label, tool,
arguments}`, built from what the finding holds and never read out of a URL.

| Action | Call |
|---|---|
| a remedy, or a workflow step's command | `execute_capability` with `name`, `target` (null where the capability takes none) and `payload`; a remedy adds its `effect` |
| an offer of its subject's own page | `get_resource` of `relationships`, the subject's node id as `identifier` |
| an investigation | `get_topology` with the subject as `focus`, and the trace `action_links.INVESTIGATIONS` declares |
| a workflow's check | the read it asks for, through `execute_capability` with its `payload`, then `get_findings` with the finding's `rule` |

A `payload` holds what HQ already knows; the agent adds an `idempotency_key`
for an effect other than `read`, and any field the capability's schema
requires. An action none of these derives is left out, as is a page elsewhere
(a connection's settings, outside documentation). `operator_steps` are the
same on both doors. The `status-brief` prompt carries the problems in this shape.

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

Always `{"ok": false, "error": {"code", "message", "details", "retry_after"}}`,
the last two only where they apply: `retry_after` is the whole seconds until
asking again can succeed, on a refusal whose code waits (`rate_limited`,
`too_many_calls`), and the `Retry-After` header repeats it. The document
declares both only at that status. A refusal the caller gets past by a call or
a field it did not make says which in `details`. `details.next` is the call
that finds what was missing: an MCP tool and its `arguments`, which the API
serves as the route of that name. That is `describe_capabilities` for an
unknown capability, or `list_resource` for a record no identifier matched,
beside `details.identifier`, the field its kind is addressed by.
`details.closest` is the call that reads each of up to three nearest the value
asked for. For a record, it is the `get_resource` of each record near its
identifier on the first page of its kind's list read as the caller, so it never
names one the caller could not list. For a resource name HQ does not serve
(`unknown_resource`, the same refusal on both doors), it is the
`describe_resources` of each resource named near it. A type of managed record
sent as a resource's name (`cloudflare.dns_record`) is answered with
`details.next` the `infrastructure` list narrowed by that `kind`, and a type of
reading (`adguard.client`) its read from `readings`. `details.field` is the
argument to send (`payload.idempotency_key`, `target`). `details.scope` is the
permission an `insufficient_scope` refusal needed: the token lacks it and its
person holds it. Granting it to the client in the identity provider and signing
in again gets the call through. The 403's `WWW-Authenticate` repeats it as
`scope` (RFC 6750 section 3.1). A permission the person's role lacks, or one no
token can hold, stays `forbidden`. Branch on the status; the code says which
cause. A code has one status wherever it is answered. `CODES` in
`hq/platform/application/refusal_status.py` declares each code once, with what
it means, its status, the doors that can answer it and whether it waits, and
every list of codes is derived from it. Each operation's response for a status
lists the codes it answers there as its `code` field's `const` or `enum`. A
capability's operation lists only what its declaration lets it answer:
`target_required` or `target_not_allowed` by whether it takes a target, and the
retry key's and a held change's codes only for a change. What a handler raises
on its own (`not_found`, `conflict`, `precondition`) no capability declares, so
every one lists those. Operations answering the same codes share one response,
named in words. What every endpoint answers at a status is named for the status
(`Forbidden`), and one code alone for that code (`InvalidInput`). Any other set
is named for the kind of call that answers exactly it, then the status
(`ReadForbidden`, `UntargetedChangeBadRequest`, `ResourceReadNotFound`,
`AnyCallNotFound` for a call by any name). Every refusal of a grant or a
policy, on any endpoint, is recorded once in the audit log as `Denied`.

An `invalid_input` message names every offending field and why, in one
sentence: `project.create: name is required.`, or
`example.decide: verdict must be one of applies, does_not_apply.` It gives field
paths, expected types and declared choices, never a value the request carried.
A field a command does not have is refused at any depth, as `extra_forbidden`
at its path (`rules[0].scope is not a known field`), and its `input_schema` is
closed at the same levels; only a part its type declares open takes any key.
`details` holds the structured list the sentence was built from, under the same
rule: `type`, `loc` and `msg` for a schema error, field names and error codes
for a domain one, and no submitted value in either. A resource's read
(`InvalidResourceInput` in `hq/platform/application/resources.py`, the one
place every door's refusal is raised) puts that list under `details.errors`,
beside `details.filters`, what the kind's list narrows by, and `details.next`,
`describe_resources` with the kind's `name`, which answers each filter's type
and choices.

### An agent's limits

An agent past its call rate is refused `rate_limited`; past its calls or
listen streams at once, or past every agent's calls at once together,
`too_many_calls`; and a tool call waited on past its timeout answers
`call_timed_out` (the `SEVERINO_AGENT_*` settings in
`hq/config/settings.py`). Each door says how long to wait in the refusal's
`error.retry_after`. The machine API answers a limit with `429` and repeats
it as `Retry-After`. `/api/mcp/` answers a tool call with an error result
carrying the code, the wait repeated in its `_meta.retry_after`, and a prompt
or resource read refused for its rate carries it in the protocol error's
`data`. The door still serves `initialize` and the tool list, since a client
drops a server whose connection is refused. A refused call runs nothing. The
rate is spent before the agent is observed, so a flood writes no row past its
refusals, and those are one audit row an agent a minute, with their count.

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
grant or the record that stopped it has changed, and a held call resent under
it is held again, at `202`.

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

This synthetic example shows the transport contract. A real extension lives
in its own repository, with its own setup guide, and its capability schema is
the source of truth for its payload.

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
that one extension capability, but cannot read unrelated records, touch a
project, or delete anything.

## Contract tooling

The host and controller descriptions pin OpenAPI 3.2.0, the version the
controller generator's parser (kin-openapi) recognizes. Move both to 3.2.1 when
it does; a patch release adds no features. No compatibility translation is
applied.

Resource filters emit individual query parameters; an unknown one is refused
`invalid_input`.

`mise run lint:openapi` installs the locked Redocly from `tools/lint` and
validates every description against the specification.

`mise run lint:openapi-breaking` compares `hq-api.openapi.json` with the base's
copy (fast's base; in CI the commit the change lands on) through oasdiff. It
fails on each change that breaks a client: a removed path, operation or
response field, a parameter or request field made required, a narrowed enum, a
changed type. Each line it prints is oasdiff's account of one break. A
deliberate break is admitted by copying that line into
`hq/platform/api/hq-api.breaking-exemptions.md`, oasdiff's own `--err-ignore`
file, followed by `Reason:` and why clients can take it; an entry with no
reason fails the gate. Once the change is on main the entry matches nothing and
is deleted. The controller's two documents are left out, since both of their
ends ship in one image.

### The document

`hq/platform/api/openapi.py` derives the document from the API's routes and
the capability, resource and domain registries, so one added anywhere appears
in it with no edit. It says each described fragment once: a fragment several
declarations share is a named type alias where its meaning lives, written as
the component of that name with every use a `$ref`. A TypedDict that extends
another is the same: the base is a component of its own, and each field the
extension inherits is a `$ref` to the base's property
(`#/components/schemas/MachineRow/properties/name`), so a field is described
where it is declared. It is never `allOf` its base, since a closed base refuses
every key its extension adds. A document holding two
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
resource tool's `name` takes only the resources the registry serves, which are
the ones the document's paths serve. What each resource holds and each filter's
type and choices are `describe_resources`' answer. So the list carries the
kinds as the schema's enum, the names of each kind's filters in
`list_resource`'s description, and the field each is addressed by in
`get_resource`'s. It never carries the catalog itself. The filters are read
from each kind's query model (`list_filters` in
`hq/platform/application/resources.py`), so an extension's kind is described as
it declares itself. With no `name`, `describe_resources` lists every kind with
the `filters` its list narrows by, leaving out `limit` and `cursor`, which page
it. With one, as `/api/v2/resources/?name=` does, it answers that kind with its
list's `query_schema`, the filters alone. Beside it is `paging`, the schema of
the `limit` and `cursor` the list takes. Those are `list_resource`'s own
arguments, which it refuses inside `filters`, and the machine API's query
parameters beside the filters. A filter that takes only some values (a status,
a dimension, a resource kind, an action item's source) declares them as a
`Literal` read from where they are declared, a model's choices or a registry.
The schema then lists them as its enum. `paging`'s `limit` declares the most a
page may ask for (`MAX_PAGE_SIZE`, unless its kind declares another). A value
outside them is a refusal naming the field, through either door. `get_topology`
and `get_findings` take their read's query model (`TopologyQuery`,
`FindingsQuery`) as their argument model. It is the one `/api/v2/topology/` and
`/api/v2/findings/` read their query string by, and the document lists as their
parameters. A choice (a lens, a direction, a rule, a severity) is a `Literal`
read from where it is declared, and a bound (`depth`, `limit`) is the model's.
So both doors publish one schema and refuse one input in one sentence. What an
absent `limit` means is each door's own: the API answers every finding, and
`get_findings` the most severe few (`finding_actions.FIRST_FINDINGS`), since
each carries its evidence and every call that acts on it, its `summary`
counting every one.

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
alone. Every tool but `execute_capability` and `export_year_summary` is
`readOnlyHint` and closed-world (`openWorldHint: false`), which a client may
use to call reads without asking or side by side, and a test calls each one and
holds it to writing no row. `export_year_summary` records the export in the
audit log and includes only public and internal documentation, including the
document IDs linked from content. Browser downloads retain the complete
documentation index. The export is closed-world, `idempotentHint` and not
`destructiveHint`, and never `readOnlyHint`. `execute_capability` runs any
capability, so it states the spec's worst case for a write (`destructiveHint`
and `openWorldHint`). It also states `idempotentHint`, because every call that
changes anything carries a retry key. A call without `idempotency_key` in its
payload is refused with `idempotency_key_required`, after authority, by the
same rule the API applies to its header ([Safe retries](#safe-retries)). A
repeat with the same key returns the first answer. A client may then repeat a
call the connection lost.

Every tool lists an `outputSchema`: an object, `anyOf` the tool's success and
the refusal envelope (`ok: false`, `error.code`, `error.message`). The spec has
every structured result meet the schema, with no exception for `isError`, and
HQ's refusals carry `structuredContent`, so the success shape alone would be
broken by every refusal. The success is the handler's return annotation, the
TypedDict the application returns (`hq/platform/mcp/services.py`). The API
document's `data` for the same read is that type too, so the two doors describe
one answer. The exceptions are the shapes that are MCP's own: `get_findings`
(`finding_actions.AgentFindings`), `dashboard_snapshot`
(`dashboard_answer.AgentSnapshot`) and `describe_capabilities`
(`capability_answer`). `describe_capabilities` answers in two levels, as an API
catalog is read. With no `name` and no `resource`, it is an index of each
capability's name, label, summary, effect, resource and `permitted`, without
its schema. Narrowed, it is each entry in full, as `/api/v2/capabilities/`
answers it. The definitions the entries' `input_schema`s share are held once in
the answer's root `$defs`, which each `#/$defs/<name>` there points to. The API
answers every entry in full with each schema whole, since its readers index one
entry's schema alone and its document and recorded examples hold that shape.
`dashboard_snapshot` answers what needs doing: the figures, the cards, the
priority queue's first page, with `priority_count` counting it whole, and the
docs due a review. For the whole queue and each other list the page shows (the
recent activity, the active projects, the draft and published content), its
`lists` names the `recent_activity` or `list_resource` call that reads it, and
leaves out one the caller may not read. Every domain, an extension's too,
raises items into the queue, so `list_resource` answers `action_items` a page
of `dashboard_answer.ACTION_ITEMS_PAGE` when no `limit` is sent, `next.cursor`
reading the rest; the API's page is every list's default, since what an absent
`limit` means is each door's own. `ok` is the door's own: the API wraps each
answer in `Ok`, and a tool's answer is the type alone, its refusal saying `ok:
false` beside `error`. A capability's result is its runner's answer, so
`execute_capability` alone carries `ok: true`.

Each schema is a document of its own and cannot reference another tool's: its
definitions are written out where they are used, without titles or descriptions
(`shapes.answer_schema`). A tool's shape is open, since a client validates each
answer by it as it comes, and a field the answer gains must not break the
client. Only a type declared with `extra="forbid"` is closed (`get_findings`'),
so a field its serializer adds fails the output-shape tests until it is
declared. A part the application writes from a dataclass
(`json_schemas.Written`, a topology node or a finding's operator steps) is
typed whole in the API document and left an object in the tool's shape. The
SDK's client checks a success against the schema and never a refusal. So
`test_output_shapes.py` calls every tool both ways on the showcase's data and
checks each answer with the client's own validator. It holds each success, its
left-out fields restored to their defaults, to its handler's type through
Pydantic, so a required key an answer lacks fails. It also fails a field a
shape names that no answer holds or leaves out.

A success leaves out each field that holds its default, as protobuf's JSON
mapping does, so an answer costs an agent only what it says. A field's
default is the one its type declares, or else its empty value: null for a
field that takes null, else empty text, `false`, or an empty list or object.
A number is a figure, so a zero is said. Each such field states its `default`
in the tool's `outputSchema` and is not `required` there. The rule reads the
answer's type, a `Written` part typed whole as the API document types it, in
the one place every success is encoded (`server._answer`, through
`shapes.omitted`). A part its type leaves untyped (a record `get_resource`
reads, a capability's JSON Schema, a map's entries) is answered as it is, and
a refusal is answered whole. `execute_capability` takes an absent `payload` as
an empty one, so a call an answer carries is sent as it reads. The machine
API answers every field: it encodes its answers itself (`views._json`), its
readers (Shortcuts, scripts) index each key, and its document and recorded
examples hold each one.

`mise run audit:mcp --base <ref>` measures what the listings cost and what an
agent's first request costs a served process. It reports the tools/list
answer's bytes and approximate tokens, each tool's part, and the bytes of the
prompt, resource and resource template lists. It reports each tool's answer on
the standard data set, with the share of it that is empty values and repeated
text, and the server's load and first answers. The API document and the SDK are
named apart, host alone and composed. A stage after those makes as many slow
calls at once as the agent limits let every agent make, and the same calls one
after another. For each it reports the resident and traced memory while they
are in flight, and the threads and calls at the peak.

tools/list names a caller only the tools it may run. Each tool declares who
that is beside its handler (`@tool(..., requires=...)`, an access from
`hq/platform/application/access.py`, in `hq/platform/mcp/services.py`), and the
binding refuses a call by the same rule, `forbidden`, before the handler runs,
so the list and the refusal never disagree. The catalogs
(`describe_capabilities`, `describe_resources`, `describe_connections`) are
listed to everyone, since each says `permitted` of every entry. `list_resource`
and `get_resource` are listed to a caller who may read some kind each serves,
and `execute_capability` to one who may run some capability. A change is held
for a person only after its caller's grant is checked, so a caller without one
has nothing to ask for. prompts/list and resources/list follow the same rule.
Each prompt and fixed view declares its access beside it (`requires` in
`hq/platform/mcp/prompts.py` and `hq/platform/mcp/resources.py`) and is listed
only to a caller it admits. `prompts/get` and `resources/read` refuse by it.
The Agents page lists as an operator's own device, the widest caller, so it
shows every tool.

Each list says how long it stays fresh, through the SDK's cache hints
(`CACHE_HINTS` in `hq/platform/mcp/server.py`). tools/list, prompts/list and
resources/list change with a release or the caller's grant, so a client may
keep them for ten minutes, privately, since only a caller that signed in
reaches them. A client that holds a `subscriptions/listen` stream hears sooner:
HQ sends it `notifications/tools/list_changed` when its tools change, through a
grant change at its admission or its person's role change. The ack honors only
`toolsListChanged`, since prompts and resources change only with a release. A
stream opens under the brakes a tool call is under. Its events are held back
while its agent is paused, blocked or on an unapproved device, and sent once
the brake lifts. It ends when its token expires, and counts against its agent's
open streams ([How much an agent may ask](AI.md#how-much-an-agent-may-ask)). A
client without a stream keeps its list for up to those ten minutes. What a new
grant opens appears when it lists again, and what a revoked grant opened stays
listed until then, while every use of it is refused. The list
resources/templates/list is the caller's grants and records, so it is stale at
once.

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
| `hq://action_items` | the whole action queue, every page of it, as `needs-me` reads it |
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
  `read` is neither listed nor answered a prompt or fixed view, and a kind it
  may not read is neither listed as a template nor read.
- The brake on agents applies to `prompts/get` and `resources/read` as it does
  to a tool call. Outside a tool call no result carries a refusal, so it is a
  protocol error, with HQ's code in `data.code` (`forbidden`, `agents_paused`).
  It is `-32602` for invalid input or a record not found, `-32603` for a
  failure, whose text stays in the log, and `-32600` otherwise. The lists are
  not braked, as the tool list is not, so a paused agent keeps its connection.
- The held requests are the caller's own, or every one for a person, as the
  action queue carries them to that caller, with the request's fields and not
  its payload.

### Held changes as MCP tasks

A client on a protocol version the SDK lists in `MODERN_PROTOCOL_VERSIONS`
may declare the `io.modelcontextprotocol/tasks` extension on a request
(`_meta["io.modelcontextprotocol/clientCapabilities"].extensions`). A call of
its that HQ holds is then answered with a task in place of the held answer: `resultType: "task"`, whose `taskId` is the hold's
`approval.id`. The SDK ships no tasks implementation, so HQ serves the
extension's methods over its holds (`hq/platform/mcp/tasks.py`) through the
low-level server's `add_request_handler`, and advertises the extension in its
capabilities. A client that declares nothing keeps the held answer.

| Hold | `tasks/get` answers |
|---|---|
| Waiting for a person | `working` |
| Approved, its operation queued or running | `working`, polled every 2,000 ms |
| Approved and applied, or approved with no operation to run | `completed`, `result` the call's answer, carrying its operation as it ended |
| Approved, and its operation failed or was cancelled | `completed`, `result` a tool error coded `operation_failed`, whose `details` is the call's answer with its operation as it ended |
| Declined | `completed`, `result` a tool error coded `declined`, with the person's note |
| Lapsed unanswered, or overtaken by a change to what it acts on | `completed`, `result` a tool error coded `expired` or `stale` |
| Withdrawn by its asker, through `tasks/cancel` or `approval.withdraw` | `cancelled`, with no `result` |

Every answer carries a `statusMessage`, since a client shows the last one it
was sent. It is the sentence the approval card and the audit log say the hold
by (`approval_review.asked`), then where it stands. While it waits, it adds the
review page's absolute address (`hq_self.site_url`, built from the declared
site host, never from the request).

A decline, a lapse, an overtaken hold and a failed apply are tool errors, not
`failed`. The extension's specification keeps `failed` for "a JSON-RPC error
during execution" and says it "MUST NOT be used for non-JSON-RPC errors"
(tasks.extensions.modelcontextprotocol.io, specification, `Task` status). HQ
answers every other refusal as a tool error.

While a person decides, `pollIntervalMs` is 30,000, the longest interval Claude
Code honors. A person answers in minutes or hours, so a sooner poll only reads
the same hold again. `ttlMs` is the hold's own window, from its request to its
expiry. Once it is settled, the controller applies it in seconds, so the poll
is 2,000 ms, and `ttlMs` is null: HQ keeps every settled hold, so the task
never lapses while its change applies.

Security properties:

- The task is the hold, read afresh at each `tasks/get`; no task state is
  kept apart from it. Its id is the hold's random UUID.
- Nothing in the extension approves or declines a hold. A hold asks for no
  input, so `tasks/update` acknowledges and changes nothing. `tasks/cancel` is
  the asker withdrawing its own hold while it waits: it runs `approval.withdraw`,
  under that command's rules and audit, and the task then reads `cancelled`,
  with no result. A hold already decided goes on as it was, since cancellation
  is cooperative (tasks.extensions.modelcontextprotocol.io, specification,
  Cancellation); either way the answer is the empty result the specification
  requires.
- The review address carries the hold's id and nothing else; reading it
  takes a person signed in to HQ.
- A task answers only the actor and interface that asked. Every task method
  refuses any other id with the same `-32602`, so an agent cannot tell
  another's task from an id nothing holds. A call that repeats what another
  agent already asked is a request of its own, held as its own task, which
  only that agent reads.
- The task methods are under the brake as `prompts/get` is, and answer
  `-32021` to a request that did not declare the extension.
