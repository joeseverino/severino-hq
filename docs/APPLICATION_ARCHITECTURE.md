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
A management command may translate flags. None may decide what a valid HQ
mutation means or write around the service.

## One operation, end to end

![A mutation is validated, locked, written, audited, and committed once; any failure rolls the whole operation back](diagrams/operation-lifecycle.png)

<sup>Diagram source:
[`operation-lifecycle.mmd`](diagrams/operation-lifecycle.mmd).</sup>

The same lifecycle applies to a browser submit, MCP tool call, or CLI command.
The adapter disappears after parsing. The application service validates,
authorizes, opens the transaction, protects against stale state, writes, audits,
and returns one stable result. The adapter only chooses how that result looks.

This is the important scaling property: a fourth interface does not create a
fourth implementation.

## Source-of-truth map

"Single source of truth" is scoped by domain. Pretending one database owns
everything would make the system less honest, not more unified.

| Domain | Source of truth | What HQ stores |
|---|---|---|
| Authored documentation | Obsidian vault | Validated metadata, relationships, and vault pointers |
| Projects, assets, expenses, workflow state | HQ database | Authoritative operational records |
| Credentials and tokens | 1Password | Nothing secret; only runtime access |
| Mutation behavior | `application/` | The one executable business contract |
| Interface presentation | Web / MCP / CLI adapter | No business state |

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

`application.documentation.sync_documentation()` is the sole manifest import
path. Web upload, MCP `sync_documentation`, and `import_docs_manifest` all call
it.

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
   capabilities, while destructive documentation pruning requires the separate
   `SEVERINO_MCP_ENABLE_PRUNE` switch as well.
6. Application services revalidate all data and own transactional writes.
7. Restricted documentation is removed from AI-facing relationship results.
8. Every successful mutation leaves an attributed audit event.

Recovery remains deliberately in-process. The CLI may call the same application
service on the server, so an MCP transport outage cannot prevent bootstrap or
repair. Sharing the service—not forcing a loopback network request—is what
prevents logic drift.

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
