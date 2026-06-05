# Changelog

All notable changes to Severino HQ.
Format roughly follows [Keep a Changelog](https://keepachangelog.com); versions
follow [SemVer](https://semver.org/) once we publish a tagged release.

## [Unreleased]

### Added

- Global HQ search at `/search/`, covering projects, content, docs, assets,
  expenses, and receipts.
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

### Changed

- Header layout now uses a fixed desktop grid with a horizontally scrollable
  nav track, keeping the brand, nav, search, and user controls on one row
  instead of wrapping into stacked text.
- Account actions (Admin, Sign out) moved into a dropdown menu under the
  username, freeing space so the full nav row fits without clipping.
- Dashboard "needs attention" and "relationship health" panels no longer
  duplicate counts: needs-attention is the workflow queue, relationship-health
  is the link/metadata readout.

## [0.1.0] — 2026-05-16

Initial v1 cut: the private operating system for Severino LLC.

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
  year-summary exports (designed for the future severino-knowledge-router
  MCP).
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
