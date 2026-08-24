# Severino HQ — roadmap

## v1 boundaries (current)

Severino HQ v1 deliberately does **not** include:

- invoices, payments, payment processing, bank login integrations
- clients, leads, consulting projects
- a customer portal, public registration, or multi-tenant behavior
- payroll, inventory management
- a WordPress plugin, public webhooks, public AI chat

The goal of v1 is to build the private operating system — the link graph —
that everything later will sit on top of.

## v2 candidates

Roughly in the order they're most likely to land.

### Knowledge router — shipped as [`severino-vault-mcp`](https://github.com/joeseverino/severino-vault-mcp)

The highest-leverage v2 idea has since shipped as its own repo: a local stdio
MCP server that reads the Obsidian vault frontmatter, the docs manifest, and
HQ's relationship-aware JSON exports, and answers questions like "what runbook
covers AdGuard?" or "what assets relate to project Y?" — behind a sensitivity
gate that withholds secret-adjacent runbook bodies. HQ supplied the
prerequisites that made it possible: stable `doc_id`s/slugs, AI-readable
exports, and the frontmatter schema (`docs_index/schema.json`) the MCP and HQ
now both validate against. The MCP runs locally on the Mac; git-crypt keys
never go on the server.

### HQ typed control plane — registry-driven execution shipped

HQ now serves a stateless Streamable HTTP MCP endpoint directly over Tailscale.
Its reads and narrow mutations come from the same resource and capability
registries used by the HTTP API, CLI, and Command Center. The endpoint is
source-network restricted, Host checked, Origin checked, and bearer
authenticated. Writes are schema validated, capability authorized, audited,
atomic, and durably idempotent. It does not wrap SSH, management commands, or
generic model access.

### Consulting & client side

- Lead / contact records
- Client records
- Consulting project records (separate model from internal projects)
- Invoices (PDF generation + ledger)
- Payments
- Tax-friendly export of paid invoices

### Public integrations

- WordPress content **pull** (read-only): mirror `published_url` /
  `wordpress_post_id` from jseverino.com so the content pipeline shows
  ground truth.
- WordPress bridge plugin (optional, later): outbound webhooks from
  Severino HQ so publishing a content item can flip the WP status.
- GitHub metadata integration: pull commit counts / last-push dates against
  projects' `repository_url`.

### Infrastructure

- Optional Postgres migration, triggered by evidence rather than fashion: more
  than one application replica or web worker needs to write, WAL still produces
  observable lock failures under normal use, backup/maintenance pauses become
  operationally significant, or a required query depends on Postgres features.
  The ORM and migrations are already DB-agnostic; SQLite-specific PRAGMAs and
  FTS/export paths are isolated behind replaceable boundaries.
- HTMX for inline edits on list pages, especially expenses and receipts.
- Bulk import for expenses (CSV).

### Quality-of-life

- Saved filters / pinned views on each list page.
- Diff-style audit log entries (snapshot before/after).
- "Quick-link" UI when creating an asset from a receipt or an expense.

## Anti-goals (probably won't ever build)

- Multi-tenant SaaS.
- Anything that requires the app to be reachable from the public internet.
- Anything that pushes secrets, runbook bodies, or receipt files into AI
  exports.
- Replacing Obsidian. The vault stays the source of truth for written
  knowledge; Severino HQ indexes pointers.
