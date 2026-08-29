# Severino HQ — machine-client API

The fourth delivery adapter, after the web UI, the CLI, and MCP. It exists so a
phone, a Shortcut, or a cron job can run an HQ capability over HTTP.

It adds **no capability, no domain model, and no business rule**. Every command
comes from `application/capabilities.py`, and every read comes from
`application/resources.py`. That keeps four adapters from drifting into four
behaviours.

```
hq_api/security.py   verify a token HQ did not issue
hq_api/views.py      the transport
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
   `SEVERINO_API_RESOURCE` (e.g. `https://hq.jseverino.com/api`). This becomes
   the `aud` claim and **cannot be changed later**.
2. Add a **Permission** for each capability the client needs, named *exactly*
   as HQ names it — `example.write`, `write_receipts`, `write_expenses`.

The permission keys are HQ's capability names on purpose. A mapping table
between the two systems would be a third home for the authorization model and
the first thing to go stale when a plugin adds a capability.

Then in **Administration → OIDC Clients**, add a client per automation, with
the client credentials grant enabled. One client per automation, not one shared
client: separate secrets rotate independently, and the token's `client_id`
becomes the actor in HQ's audit log, so an import is traceable to the
credential that caused it.

## Using it

Get a token:

```bash
curl -s https://sso.jseverino.com/api/oidc/token \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" \
  -d resource="https://hq.jseverino.com/api" \
  -d scope="example.write"
```

Ask what it may do:

```bash
curl -s https://hq.jseverino.com/api/v2/ -H "Authorization: Bearer $TOKEN"
```

```json
{"ok":true,"data":{"actor":"example-automation","granted":["example.write"],...}}
```

Run a capability:

```bash
curl -s https://hq.jseverino.com/api/v2/capabilities/example.import/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"payload":{"records":[{"external_id":"sample-1","value":42}]}}'
```

### Routes

| Method | Path | |
|---|---|---|
| `GET` | `/api/v2/` | Who you are and what you were granted |
| `GET` | `/api/v2/capabilities/` | Every capability, flagged `permitted` for this token |
| `POST` | `/api/v2/capabilities/<name>/` | Run one |
| `GET` | `/api/v2/resources/` | Every read resource, its operations and filter schema |
| `GET` | `/api/v2/resources/<name>/` | List a resource using validated query parameters |
| `GET` | `/api/v2/resources/<name>/<identifier>/` | Get one addressable record |
| `GET` | `/api/v2/connections/` | Connection families, abilities, scope coverage, and safe cached state |
| `GET` | `/api/v2/topology/` | The permitted live graph, optionally narrowed by lens or a bounded dependency trace |
| `GET` | `/api/v2/findings/` | Evidence-backed claims with stable IDs, causal rollups, authorized remedies, and derived understand → act → verify workflows |

`/api/` is exempt from the session-login redirect but **not** from
authentication. An anonymous request gets `401` with a `WWW-Authenticate`
header, never a 302 to an HTML login page — a Shortcut cannot fill one in, and
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
`missing_days` means HQ has not read that site-day—not that no visit occurred.
The controller discovers sites, asks HQ for bounded missing windows, and
backfills them idempotently; API and web readers do not invent their own
freshness policy.

### Compatibility policy

The path is the semantic major version. Additive fields may join an existing
version; removing a field, tightening accepted input, or changing retry
semantics requires a new path. Version 1 remains available for the original
Shortcut contract and returns `Deprecation: true` plus a `successor-version`
link. Version 2 is the current contract and requires durable idempotency for
state changes. No sunset date is advertised until there is an actual removal
decision and migration window; clients are never given a fictional deadline.

### Errors

Always `{"ok": false, "error": {"code", "message", "details"}}`.

| Status | Means |
|---|---|
| `401` | No token, or it failed verification. Mint a new one. |
| `403` | Verified, but this client was not granted that capability. Fix its scope. |
| `404` | No such capability on this deployment. |
| `409` | The domain refused the command, or a retry key was reused with different input. |
| `413` | The request exceeds the deployment's body-size safety limit. |
| `415` | A capability request was not sent as `application/json`. |
| `503` | `SEVERINO_API_RESOURCE` is unset here. |

### Safe retries

Every capability whose effect is not `read` requires an `Idempotency-Key`
header. Generate one opaque key per logical operation and keep it unchanged
when retrying that operation. HQ stores the actor, canonical request hash, HTTP
status, and response in the same database transaction as the domain write. A
retry therefore receives the committed response without running the command a
second time—even after a process restart. Reusing the key with different input
returns `409 idempotency_conflict`.

Records expire after 24 hours by default and expired records are pruned by the
next machine write. Configure the window with
`SEVERINO_API_IDEMPOTENCY_TTL_SECONDS`. Domain-level idempotency remains useful:
it protects imports arriving through web, CLI, or MCP, while this transport
contract protects an HTTP client that did not receive the first response.

## Recipe: a narrowly scoped first-party automation

This synthetic example demonstrates the transport contract without placing a
private plugin's domain vocabulary or workflow in the public host repository.
The deployed plugin's capability schema is the source of truth for its real
payload; its private repository owns the corresponding setup guide.

In an automation client:

1. Produce the source records for one logical operation.
2. Request a token from `https://sso.jseverino.com/api/oidc/token`, POST,
   `Form` body: `grant_type=client_credentials`, `client_id`, `client_secret`,
   `resource=https://hq.jseverino.com/api`, `scope=example.write`.
3. Read `access_token` from the result.
4. Generate a UUID and retain it as the operation's retry key.
5. POST to
   `https://hq.jseverino.com/api/v2/capabilities/example.import/`, with
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
