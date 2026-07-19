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

### HQ typed control plane — read foundation shipped

HQ now serves a stateless Streamable HTTP MCP endpoint directly over Tailscale.
Its first tool surface is deliberately read-only: projects, assets, expenses,
receipt metadata, documentation status, recent activity, and health. The
endpoint is source-network restricted, Host checked, Origin checked, and
bearer authenticated. It does not wrap SSH or management commands.

Narrow mutation tools remain a later phase. They require a shared service
layer, idempotency keys, validation previews, structured errors, and an
explicit MCP audit event before they ship.

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

- Optional Postgres migration. The ORM and migrations are already DB-agnostic;
  the v1 SQLite settings include explicit `init_command` PRAGMAs we'd drop on
  Postgres, and any SQLite-specific export paths would need a small refactor.
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
