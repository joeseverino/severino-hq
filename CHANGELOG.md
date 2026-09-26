# Changelog

All notable changes to Severino HQ. The format follows
[Keep a Changelog](https://keepachangelog.com), and versions follow
[SemVer](https://semver.org/): the plugin API, `hq_sdk`, and the API and MCP
contracts are the public surface.

## [1.0.0] - 2026-09-26

HQ derives the estate from its connections. Connect a Cloudflare token and a
Tailscale credential, and machines, services, domains and the relationships
between them are read, joined and kept current, with every write going through
one gated path.

### Added

- Readings: one observation contract (`control_plane/observations/`). A schema
  is the allowlist for what is stored, each kind has one reader, and a refused
  read says which permission is missing. Cloudflare (DNS, Pages, D1, Access
  applications and service tokens, tunnels, edge certificates, zone settings,
  registrar), Tailscale (devices, DNS, settings, users, policy) and public
  registries (RDAP, on an hourly timer).
- A join engine and relation graph: machines, services and domains as nodes,
  readings as labelled edges. Machine, service and domain pages, the topology,
  search and the dashboard estate card all read the same graph.
- Derived facts: machine roles (exit node, tailnet DNS), a machine's own LAN
  and public addresses from what the whole tailnet reports, HQ's own machine
  and port, and relationships shown from both ends.
- Derived resource capabilities: what a page offers follows the controller
  policy, the resource's state, and whether its connection manages.
- Onboarding: the connections page shows what each credential can see and
  which permission would widen it, with scripts that mint least-privilege
  Cloudflare and Tailscale credentials, and a one-time `hq.import` for what
  cannot be derived.
- Machine surfaces for every derived read: estate, action items, machines,
  domains, relationships, readings, credentials and search are registered once
  and served by the API, MCP, CLI and SDK.
- A schema-driven infrastructure control plane shared by the web UI, API, MCP
  and CLI: typed desired resources, leased operations, drift and expiry
  signals, and a controller that reconciles through declared capabilities.
- An approval gate: changes to the most sensitive kinds, asked for by a
  credential rather than a person at a browser, are held until a person
  agrees.
- A plugin host: extensions are admitted as signed wheels and composed into
  the deployed image; the host names none of them.

### Changed

- A record is adopted, reconciled, renewed or removed only through a
  connection that declares `manages`. HQ enforces this on every write path and
  the controller enforces it again. "Stop managing" forgets a record without
  touching the provider.
- One read projection per page request: the machine catalogue, relation graph
  and readings are built once and shared.
- A page GET never writes or calls an outside service. Refreshes are POSTs or
  timers.
- Audit events carry the connection they came from; routine probe events
  expire after 30 days.
- Serving moved to ASGI/Uvicorn so the web UI and the MCP endpoint share one
  lifecycle.

### Security

- Controller requests that carry credentials refuse redirects to another
  origin and always verify TLS; a refused credential is asked once per sweep.
- Audit activity requires `READ_AUDIT_LOG` on every surface.
- An unset OIDC issuer refuses sign-in rather than accepting one.
- Trusted Types and a strict Content Security Policy; one audited policy turns
  a response into markup.
- The trusted network is the tailnet and loopback, never the private LAN.
- Session and CSRF cookies use the `__Host-` prefix; HSTS is on by default.
- The web container runs with a read-only root filesystem.
- Host and composed images are cosign-signed and verified before they run.

## [0.1.0] - 2026-05-16

The first cut: a private operations app.

### Added

- Django 5 + SQLite scaffold with `core` (audit log, middleware, dashboard),
  `projects`, `content`, `docs_index`, `assets`, `expenses`, `receipts`,
  `reports` apps.
- Authentication: login-required on every URL except `/accounts/login/` and
  `/static/`. No public registration.
- Dashboard with YTD KPIs (expenses total, estimated deductible, active
  projects/assets, draft content, docs needing review, recent activity).
- CRUD UI for projects, content items, documentation records, assets,
  expenses, receipts, with search, filter, sort, pagination.
- Auto-computed `estimated_deductible_amount = total_cost * business_use_pct`
  for assets and expenses.
- Receipts: random UUID filenames, storage outside app code, no public URL,
  auth-gated streaming download view.
- Documentation manifest importer (CLI + web upload) for syncing Obsidian
  metadata into the docs index without storing runbook bodies.
- Reports page + CSV exports (expenses / assets / content / projects /
  documentation), plus relationship-aware JSON and AI-readable Markdown
  year-summary exports (designed for the severino-vault-mcp server).
- Audit log via signals + middleware on every create / update / delete /
  login / logout / login-failed / upload / export / import.
- Demo seeder (`manage.py seed_demo`) and manifest importer
  (`manage.py import_docs_manifest`).
- Production security defaults: SECRET_KEY required at startup in prod,
  ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS from env, secure cookies, secure
  headers, SQLite WAL + foreign-keys ON.
- Dockerfile (non-root UID 10001, multi-stage, healthcheck),
  docker-compose.yml that binds to `127.0.0.1:8000` only, named volumes
  for db / media / exports / staticfiles, `entrypoint.sh` auto-migrate +
  collectstatic.
- `scripts/backup.sh`: SQLite `VACUUM INTO` snapshot, tarballed with media
  + exports, optional `age` encryption.
- Docs: `README`, `docs/DEPLOYMENT.md` (Docker on homelab + systemd/Caddy
  fallback), `docs/SECURITY.md`, `docs/BACKUP.md`, `docs/ROADMAP.md`.

### Security

- DEBUG off in production (startup error if `DJANGO_SECRET_KEY` is missing).
- Audit logging on every important action.
- Documentation index is metadata-only; sensitivity labels gate AI-safe
  exports.
- Receipt files never publicly URL-addressable.

[1.0.0]: https://github.com/joeseverino/severino-hq/releases/tag/v1.0.0
