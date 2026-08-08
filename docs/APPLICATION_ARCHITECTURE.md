# One HQ brain, every interface

Severino HQ presents three faces: a fast operator web UI, typed MCP tools for
agents, and a dependable CLI for shell workflows and recovery. They are not
three implementations. They are three adapters over one application core.

![Web, MCP, and CLI converging on one application core and the domain-specific sources of truth](diagrams/application-core.png)

<sup>Diagram source:
[`application-core.mmd`](diagrams/application-core.mmd), rendered with the
canonical [`diagram`](https://github.com/joeseverino/tools/tree/main/bin/diagram)
tool.</sup>

## The boundary

The `application/` package is HQ's behavior boundary. It owns everything that
must remain identical no matter who initiated an operation:

| Concern | Canonical owner |
|---|---|
| Request shape | Typed application command |
| Field and relationship validity | Application service + Django model contract |
| Authorization policy | Application service |
| Idempotency and stale-write protection | Application service |
| Transaction and locking boundary | Application service |
| Persistence | Django ORM inside that transaction |
| Actor and interface attribution | Shared audit context |
| Returned data | Canonical result object |
| HTML, MCP JSON, terminal prose | Delivery adapter only |

A Django view may translate a form. An MCP tool may translate typed arguments.
The `hq` wrapper may translate ergonomic flags into the advertised JSON Schema.
None may decide what a valid HQ mutation means or write around the service.

## One operation, end to end

![A mutation is validated, locked, written, audited, and committed once; any failure rolls the whole operation back](diagrams/operation-lifecycle.png)

<sup>Diagram source:
[`operation-lifecycle.mmd`](diagrams/operation-lifecycle.mmd).</sup>

The same lifecycle applies to a browser submit, MCP tool call, or CLI command.
The adapter disappears after parsing. The application service validates,
authorizes, opens the transaction, protects against stale state, writes, audits,
and returns one stable result. The adapter only chooses how that result looks.

That boundary includes reads. Canonical query projections live in
`application/read_models.py`; web, MCP, and CLI adapters may filter or render
those results, but they do not import Django models or rebuild result shapes.
The projections opt into Django 6.1's `FETCH_RAISE` mode after declaring their
`select_related()` plans. An omitted relationship therefore fails at the
projection boundary instead of silently becoming an N+1 query in production.
An architecture fitness test rejects direct model access from the MCP service
adapter so this separation cannot silently regress.

The dashboard follows the same contract. `application/dashboard.py` emits one
JSON-safe operating snapshot for KPIs, priority work, recent records, upstream
state, and activity. The web dashboard renders it, while the authenticated MCP
exposes it as `dashboard_snapshot`. Infrastructure reads are likewise shared:
`list_managed_resources` and `get_managed_resource` return public desired state,
health, and structured operation evidence without provider credentials. A
future REST/OpenAPI adapter can publish these same use cases without moving or
reimplementing their behavior.

This is the important scaling property: a fourth interface does not create a
fourth implementation.

## Search and table reads

`application.search` is the search boundary for web tables, CLI/TUI clients,
and future MCP tools. It accepts a named scope and query and returns stable
domain identifiers; adapters do not know how text is indexed.

Every entry point requires a `Principal`. Ordinary scopes need the baseline
`READ` capability; the `audit` scope needs `READ_AUDIT_LOG`, which
least-privilege adapter principals (MCP) do not hold — free-text search over
the security log is an operator-only capability. A new adapter therefore
cannot expose search without deciding whose authority it acts under.

`global_search` is the cross-scope use case behind the `/search/` page:
relevance-ranked hits per scope with FTS5 `snippet()` match extracts,
returned as structured `(text, is_match)` parts so each renderer escapes
content and applies markup independently. Presentation metadata (group
label, title field, badge, timestamp) lives on each `SearchDefinition`, so
every surface labels a hit the same way. Scopes a principal cannot search
are omitted from the result, not rendered empty. Contact submissions live
in Cloudflare D1, not the local database, so the web view merges them as an
eighth group beside the registry scopes.

`search_index.SearchDocument` is a derived relational projection. On SQLite,
an FTS5 external-content table indexes that projection with Unicode tokenization
and 2/3/4-character prefix indexes. Database triggers keep the FTS structure
atomic with projection writes, while domain-model signals keep the projection
atomic with the authoritative record. A rollback therefore removes all three
changes together.

The backend is replaceable. A future PostgreSQL deployment can supply a native
search backend without changing table views, query parameters, CLI output, or
domain models. When an indexed backend is unavailable, the application service
retains a bounded ORM fallback.

Operational interfaces use the same contract:

```bash
python manage.py search_hq projects "certificate automation"
python manage.py rebuild_search_index
```

`application.tables.TableListMixin` composes indexed search with multi-value
filters, workflow toggles, allowlisted ordering, and database pagination. The
browser progressively enhances that GET contract with debounced, cancelable
requests; plain links and forms remain the complete fallback.

## Emit once, derive everywhere

![One typed command declaration deriving JSON Schema, validation, MCP and CLI surfaces, and parity tests](diagrams/emit-once-capabilities.png)

<sup>Diagram source:
[`emit-once-capabilities.mmd`](diagrams/emit-once-capabilities.mmd).</sup>

HQ's allowlisted capability registry binds each typed command to one operation
name, effect, required permissions, and application handler. From that registry:

- `describe_capabilities` emits deterministic JSON Schemas for MCP clients;
- `execute_capability` validates a JSON object and returns one canonical
  success/error envelope;
- the authenticated Streamable HTTP MCP endpoint exposes that catalog and
  executor to the web-independent CLI and agents;
- management commands remain an in-process break-glass adapter over the same
  registry; and
- tests derive their contract assertions from the emitted schemas.

The generic executor is not generic database access. It can invoke only
allowlisted operations in the registry, and every operation still crosses the
typed principal, capability check, and application transaction.

## Source-of-truth map

"Single source of truth" is scoped by domain. Pretending one database owns
everything would make the system less honest, not more unified.

| Domain | Source of truth | What HQ stores |
|---|---|---|
| Authored documentation | Obsidian vault | Validated metadata, relationships, and vault pointers |
| Projects, assets, expenses, workflow state | HQ database | Authoritative operational records |
| Credentials and tokens | 1Password | Nothing secret; only runtime access |
| Mutation behavior | `application/` | The one executable business contract |
| Interface presentation | Web / MCP / `hq` wrapper | No business state |
| Infrastructure identities and dependencies | Severino Labs topology | Trusted checksummed snapshot + stable references |
| Provider authored/resolved contracts | Provider definition registry | No parallel resolver schema |
| Controller actions and automation | Validated controller capability document | Queued operations and observations |

The vault emits a validated manifest; HQ never walks the vault and never stores
Markdown bodies. The MCP does not become a database or a second rules engine.
It exposes the same application capabilities used in-process by HQ itself.

## Reference vertical slices

### Projects

`application.projects.save_project()` is the sole project create/update path.
The web create and edit views, MCP `create_project` / `update_project` tools,
and `create_project` management command all call it and receive the same
canonical representation.

Project writes provide:

- Django field and uniqueness validation;
- an atomic transaction;
- row locking for updates;
- optional `expected_updated_at` conflict detection;
- stable relationship-safe serialization; and
- audit metadata naming the interface, actor, and operation.

### Documentation synchronization

`application.sync.execute_hq_sync()` is the external synchronization boundary.
The local Vault MCP emits the manifest and topology once; `hq sync` sends both
in one `hq.sync` MCP capability call. HQ applies both inside one database
transaction, so invalid topology cannot leave newly imported documentation
behind. Lower-level documentation/topology services remain reusable in-process.

The sync is:

- atomic and safe to repeat;
- preflight-validated against the canonical frontmatter contract;
- bounded to 2,000 JSON-object records;
- incapable of importing Markdown bodies; and
- fail-closed for deletion: pruning requires `prune_orphans=true` and the
  separate `confirm_prune=true`.

### Assets

`application.assets.save_asset()` extends the same contract to equipment and
financial metadata. Web create/edit, MCP `create_asset` / `update_asset`, and
the `create_asset` management command share one transaction and result shape.
The service resolves project relationships before writing, rolls back on any
missing slug, normalizes deductible values through the model contract, and
supports the same optional stale-write protection as Projects.

### Content

`application.content.save_content()` owns the publishing pipeline record and
its Project, Asset, Expense, and Documentation relationships. All relationship
identifiers resolve before persistence, so one missing reference rolls the
entire operation back. MCP results omit sensitive and restricted documentation
identifiers while the authenticated web UI can still manage the underlying
relationship through the same service.

### Expenses

`application.expenses.save_expense()` owns financial record creation and
updates, deductible calculation, and its optional Project, Asset, Content, and
Documentation links. Related identifiers resolve before persistence, updates
lock the row, and MCP/CLI results share the same money-as-string representation
without disclosing sensitive documentation identifiers.

### Receipts

Receipt files and receipt metadata deliberately have different ingress paths.
Authenticated web upload calls `application.receipts.upload_receipt()` and the
shared file policy before private storage. JSON/MCP exposes only
`receipt.update`, which can change metadata and stable Expense/Asset links but
can never read, upload, replace, or return file bytes or a storage path. The
schema-derived capability therefore stays plug-and-play without turning the
MCP into a file-exfiltration surface.

### Documentation records

`application.documentation.save_documentation()` owns manual documentation
metadata creation and updates. It resolves all Project, Asset, and Expense
relationships before persistence and returns a sensitivity-aware canonical
representation. Restricted records remain manageable in the authenticated web
UI, while MCP and CLI results redact their vault, repository, URL, and notes
pointers.

### Deletes

`application.deletion` owns deletion for all six mutable HQ record families.
Every delete is an explicit registry capability with a `destructive` effect,
requires an exact target confirmation, locks the current row, optionally checks
`expected_updated_at`, and emits the normal attributed audit event. Receipt
storage cleanup runs only after the database transaction commits. MCP deletion
requires both ordinary writes and the separate delete switch; the CLI remains
an in-process recovery path.

## Security model

The service boundary complements the existing network boundary:

1. The MCP endpoint exists only on the tailnet.
2. `MCPBoundary` validates the direct peer, Host, Origin, and strong bearer
   token before tool dispatch.
3. Tools expose task-shaped capabilities, never generic SQL or arbitrary model
   mutation.
4. A typed `Principal` carries explicit capabilities into the application
   service; the service—not the adapter—authorizes the operation.
5. MCP starts read-only. `SEVERINO_MCP_ENABLE_WRITES` enables ordinary mutation
   capabilities. Destructive documentation pruning additionally requires
   `SEVERINO_MCP_ENABLE_PRUNE`; record deletion additionally requires
   `SEVERINO_MCP_ENABLE_DELETES`.
6. Application services revalidate all data and own transactional writes.
7. Restricted documentation is removed from AI-facing relationship results.
8. Every successful mutation leaves an attributed audit event.

Routine CLI domain operations use the authenticated MCP endpoint: synchronization,
registry validation, project/asset upsert, and report export. SSH is reserved
for host administration—deployment, logs, restart, shell, superuser, and secret
refresh. In-process management commands remain break-glass recovery paths, but
the normal CLI cannot silently become a second transport or rules engine.

## Adding the next capability

Every new write follows one mechanical path:

1. Define the typed command and canonical result in `application/`.
2. Implement validation, authorization, locking, persistence, and audit there.
3. Add minimal web, MCP, and CLI adapters.
4. Prove identical result shapes with an adapter-parity test.
5. Prove rollback and the relevant idempotency, permission, and conflict cases.
6. Document the capability here once it joins the supported surface.

Business logic in a view, MCP registration function, or management command is
an architecture regression and should fail review.
## Infrastructure control plane

Severino Labs topology owns stable infrastructure identities and dependency
edges. HQ imports the complete sensitive topology into a trusted, checksummed
server-side snapshot. Managed resources reference topology identities; they do
not duplicate hosts, certificate SANs, or consumer topology.

HQ stores typed operational intent, resource generations, public observations,
and audited operation requests. Each provider definition owns its authored
schema, reference resolver, and resolved runtime schema. Topology supplies the
trusted graph; it contains no TLS-specific resolver logic. Web, MCP, scheduler,
and controller contracts consume the same resolved provider output. HQ does not
store provider credentials or private keys. Every interface invokes the same
application capabilities.

Controllers claim operations with an expiring lease and receive a minimal,
versioned, desired-only JSON contract. A controller resolves runtime connection
references, executes provider adapters, verifies each declared consumer, and
reports only public status and conditions. Expired claims return to the queue;
only one queued or claimed operation may exist for a resource/action pair.

The homelab controller is a separate root-owned systemd oneshot, not a web
process. It starts a disposable, capability-dropped container from the exact
scanned HQ image, so the host needs no parallel Python environment and cannot
drift from the deployed application. Provider variables, the ACME lineage, and
deployment identities enter only that short-lived container; they never enter
the web container. The disposable container runs as the same unprivileged UID
as the application data owner; the root-owned systemd launcher projects
short-lived, owner-scoped copies of its environment and SSH identities. Plan
mode authenticates and peeks without leasing work.
Apply mode first schedules due work, then claims only explicitly supported
kind/action pairs. The validated capability document declares which actions are
automatic; a generic scheduler derives reconciliation for generation/health
drift, while the TLS provider adds expiry-window renewal policy. The worker
imports the same validated registry and dispatches every declared action through
one provider/action map. AdGuard and NPM reconcile in apply mode. TLS reconciliation reuses the
existing lineage without contacting ACME. For NPM, one managed certificate is
uploaded once and every enabled proxy host covered by its SANs is discovered,
rebound, reloaded, and live-verified. TLS renewal issues through DNS-01 only
when necessary, snapshots the rollback artifact, deploys to all consumers, and
verifies one fingerprint everywhere before reporting success. Public DNS
remains locked. Short-lived NPM tokens stay in memory and reports are rejected
if they contain secret-bearing keys.

HQ's existing `CLOUDFLARE_API_TOKEN` is application data-plane access for the
D1-backed contact form. It is never projected into the controller or reused for
DNS automation. DNS-01 uses the separate least-privilege
`cloudflare-dns-jseverino` connection.

![Infrastructure control plane](diagrams/infrastructure-control-plane.png)
