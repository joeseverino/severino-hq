# Changelog

All notable changes to Severino HQ.
Format roughly follows [Keep a Changelog](https://keepachangelog.com); versions
follow [SemVer](https://semver.org/) once we publish a tagged release.

## [Unreleased]

### Added

- A schema-driven infrastructure control plane shared by the web UI, CLI, and
  MCP: trusted topology snapshots, typed desired resources, leased operations,
  provider-safe status, certificate drift/expiry priority signals, and public
  certificate downloads.
- A homelab-server controller with declarative capability and connection
  registries. AdGuard rewrites, Nginx Proxy Manager hosts, and TLS consumer
  observation are active; certificate renewal and public DNS remain visibly
  fail-closed until their least-privilege credentials and deployment identities
  are provisioned.
- Gated controller deployment from the exact scanned HQ image, including
  provider preflight, root-only secret projection, systemd scheduling,
  action-filtered claims, health rollback, and architecture diagrams.
- Canonical `application/` services shared by web, MCP, and CLI, with project
  create/update for Projects, Assets, Content, and Expenses plus documentation
  sync as reference vertical slices.
- MCP project mutations and fail-closed documentation synchronization, backed
  by adapter-parity, rollback, concurrency, idempotency, and pruning tests.
- Typed application principals and capabilities. MCP mutations are disabled by
  default, with destructive pruning gated separately from ordinary writes.
- An allowlisted JSON capability registry that derives deterministic JSON
  Schemas, validation, effects, MCP execution, CLI execution, and parity tests
  from the typed command declarations.
- Receipt metadata updates now use the same schema-derived capability system;
  binary upload remains an authenticated web-only ingress with one shared file
  size/type policy and no MCP file or path exposure.
- Documentation metadata CRUD and explicit confirmed deletes now route through
  the same application services and capability registry. Delete schemas,
  permissions, effects, validation, MCP execution, and CLI execution derive
  from one declaration; MCP deletes remain separately fail-closed.
- Tailnet-only Severino HQ MCP control plane using stateless Streamable HTTP.
  The initial typed, read-only tools cover projects, assets, expenses, receipt
  metadata, documentation status, recent activity, and system health.
- A fail-closed MCP ASGI boundary: direct Tailscale peer enforcement, explicit
  Host validation, Origin rejection by default, strong bearer authentication,
  and no trust in forwarded client-address headers.
- Gated CI/CD pipeline (`.github/workflows/ci.yml`): lint (ruff), tests on
  Python 3.12/3.13, a `check --deploy` posture gate, `pip-audit`, then a GHCR
  image build that Trivy scans. On green, a self-hosted runner on the homelab
  pulls the scanned image and restarts the container — `docker compose pull`
  instead of an on-box build, so the artifact that deploys is the one that was
  tested. All actions are SHA-pinned. `docker-compose.yml` takes a
  `SEVERINO_IMAGE` override for the pull-by-tag deploy.
- Global HQ search at `/search/`, covering projects, content, docs, assets,
  expenses, and receipts.
- `docs_index/schema.json` + `docs_index/frontmatter_schema.py`: the frontmatter
  enum contract is now single-sourced from the MCP's `schema.py` (emitted via
  `severino-vault-mcp schema --json`, regenerated with `hq schema`). The
  manifest importer validates against it instead of model `.choices`, so HQ can
  no longer reject a value the MCP just wrote. `docs_index/tests.py` guards both
  the committed JSON (vs the installed MCP) and the model `TextChoices` (vs the
  schema).

- `audit_registry` management command (`--json`): the read-only Project/Asset
  registry-orphan audit that `hq validate` used to run as an inline ORM script
  piped into `manage.py shell` over SSH. Now a real, tested command.

### Changed

- Project and documentation writes now use shared transactional use cases with
  interface-aware audit metadata.
- Production serving moved from WSGI/Gunicorn to ASGI/Uvicorn so the Django UI
  and Streamable HTTP MCP endpoint share one lifecycle.
- `import_docs_manifest` validation now derives allowed doc_type / environment /
  status / sensitivity from the shared schema rather than the model's
  `TextChoices`, closing the latent drift where the MCP accepted `environment:
  lab` / sensitivity aliases that HQ rejected.
- Documented the `import_manifest_data` stats dict as the explicit contract the
  `hq sync` wrapper parses (keys are additive, not to be renamed).
- Optional Pocket ID / OIDC SSO for HQ. Password login remains available as
  break-glass; OIDC users must match an allowed email or allowed group.
- Pocket ID account linking now uses `preferred_username` first and does not
  require an email claim for users authorized through the `admins` group.
- HQ keeps PKCE enabled alongside its OIDC client secret; PKCE requirements
  are relying-party-specific and must not be inferred from Portainer.
- Dashboard needs-attention queue linking to filtered cleanup views for docs
  needing review and draft content.
- Dashboard quick actions for common create/import flows.
- Relationship health counts on the dashboard.
- Active navigation state in the main header.
- Trusted Types across the application. Assigning a string to a DOM sink now
  throws rather than parsing; one named policy, `hq-fragment`, is the single
  audited place a same-origin response body becomes markup, and duplicates are
  refused so injected script cannot mint a second one. Django admin runs
  without that directive and only that directive.
- `/csp-report/` records what the browser refused, with a bounded body,
  truncated fields, and one row per distinct complaint per hour. It is the only
  way HQ learns that a policy enforced in someone else's browser stopped
  holding.
- Two connection layers: whether there is exactly one encrypted way in
  (HTTPS redirect plus HSTS), and what the page is allowed to run. The protocol
  panel now names the individual directives the browser was sent, read back
  from the response it actually received.

### Changed

- The trusted-network default is the tailnet and loopback. RFC 1918 is no
  longer shipped as trusted: a LAN holds printers, televisions and guests, and
  a host firewall that is the only thing enforcing the rule is one command away
  from silently admitting all of it. A deployment whose network genuinely is
  the boundary now says so explicitly.
- `DJANGO_BEHIND_TLS_PROXY` also turns on the redirect to the canonical HTTPS
  name, so the plain port HQ binds stops being a second front door. The
  healthcheck path is exempt; `security.W008` is now silenced only where the
  redirect is genuinely off.
- HSTS defaults to a year including subdomains, rather than off pending manual
  enablement that outlived the reason for it.
- Session and CSRF cookies carry the `__Host-` prefix wherever they are Secure.
- `Cross-Origin-Resource-Policy: same-origin` on every response, including the
  static mount that sits above the Django middleware.
- The web container runs with a read-only root filesystem, a tmpfs `/tmp`, and
  a memory limit, alongside the capability and privilege restrictions it
  already had.
- The host image and the composed image are cosign-signed by the workflows that
  build them; the composition verifies the host image before building on it and
  the deploy verifies the composition before recreating the container.
- Static assets are never far-future cached in development. The version token
  hashes the source tree while the mount serves the collected one, so an
  edit-then-load could pin stale bytes under a fresh URL permanently.

- Header layout now uses a fixed desktop grid with a horizontally scrollable
  nav track, keeping the brand, nav, search, and user controls on one row
  instead of wrapping into stacked text.
- Account actions (Admin, Sign out) moved into a dropdown menu under the
  username, freeing space so the full nav row fits without clipping.
- Dashboard "needs attention" and "relationship health" panels no longer
  duplicate counts: needs-attention is the workflow queue, relationship-health
  is the link/metadata readout.

## [0.1.0] — 2026-05-16

Initial v1 cut: the private operating system for Severino Labs.

### Added

- Django 5 + SQLite scaffold with `core` (audit log, middleware, dashboard),
  `projects`, `content`, `docs_index`, `assets`, `expenses`, `receipts`,
  `reports` apps.
- Authentication: login-required on every URL except `/accounts/login/` and
  `/static/`. No public registration.
- Dashboard with YTD KPIs (expenses total, estimated deductible, active
  projects/assets, draft content, docs needing review, recent activity).
- CRUD UI for projects, content items, documentation records, assets,
  expenses, receipts — with search, filter, sort, pagination.
- Auto-computed `estimated_deductible_amount = total_cost × business_use_pct`
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
- `scripts/backup.sh` — SQLite `VACUUM INTO` snapshot, tarballed with media
  + exports, optional `age` encryption.
- Docs: `README`, `docs/DEPLOYMENT.md` (Docker on homelab + systemd/Caddy
  fallback), `docs/SECURITY.md`, `docs/BACKUP.md`, `docs/ROADMAP.md`.

### Security

- DEBUG off in production (startup error if `DJANGO_SECRET_KEY` is missing).
- Audit logging on every important action.
- Documentation index is metadata-only; sensitivity labels gate AI-safe
  exports.
- Receipt files never publicly URL-addressable.

[Unreleased]: https://github.com/joeseverino/severino-hq/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joeseverino/severino-hq/releases/tag/v0.1.0
