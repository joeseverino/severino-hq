# Severino HQ — roadmap

## v1 boundaries (current)

Severino HQ v1 deliberately does **not** include:

- invoices, payments, payment processing, bank login integrations
- clients, leads, consulting projects
- a customer portal, public registration, or multi-tenant behavior
- payroll, inventory management
- a WordPress plugin, public webhooks, public AI chat
- a full MCP server

The goal of v1 is to build the private operating system — the link graph —
that everything later will sit on top of.

## v2 candidates

Roughly in the order they're most likely to land.

### Knowledge router (highest leverage)

A local MCP server, `severino-knowledge-router`, running on the Mac. It would
read:

- `docs_manifest.json` exported from the Obsidian vault
- Severino HQ JSON exports (`year-summary-<year>.json`) or, later, a private
  Severino HQ read-only API
- local Obsidian metadata (file list, frontmatter)

…and answer questions like:

- "What project relates to this topic?"
- "What Obsidian runbook should I read for AdGuard issues?"
- "What documentation record exists for system X?"
- "What assets or expenses relate to project Y?"
- "What content items came from this lab?"
- "What is the source of truth for this topic?"

v1 has the prerequisites: stable slugs and `doc_id`s, relationship-aware
JSON exports, AI-readable Markdown exports, sensitivity labels so secret-
adjacent runbooks are excluded. Git-crypt keys never go on the VPS; the MCP
runs locally on the Mac.

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
- Private read-only API for the MCP (bearer-token over Tailscale).
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
