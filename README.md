# Severino HQ

The private internal operating system behind Severino LLC.

Severino HQ connects projects/labs, content ideas, documentation index records,
assets, expenses, receipts, basic reports, and AI-readable exports — so a
single source of truth links a router purchase to the expense, the receipt,
the project it powers, the article it inspired, the runbook that documents it,
and the year-end summary it rolls up into.

This app is **not** the public website, a SaaS product, a CRM, or an
accounting system. It runs on the homelab / a small Linux VPS, accessible only
over Tailscale.

---

## Stack

- Django 5 + SQLite (PostgreSQL is a future option)
- Django templates (HTMX hook left in `base.html` for future use)
- Plain CSS (no build step, no CDN runtime dependencies)
- Django auth, Django admin, Django ORM and migrations
- Environment variables for secrets

## Modules

1. Dashboard — KPIs, recent activity, docs needing review.
2. Projects / Labs — CRUD with category/status, technologies, repo & public URLs.
3. Content Pipeline — CRUD with type, status, WordPress IDs, related records.
4. Documentation Index — metadata + relationships only; Obsidian stays the source of truth.
5. Assets / Equipment — purchase data + auto-computed estimated deductible.
6. Expenses — categorized line items + auto-computed estimated deductible.
7. Receipts — uploaded outside app code, served only via auth-protected view.
8. Reports / Exports — KPI page + CSV exports + year-summary JSON & Markdown.
9. Audit Log — every important create/update/delete/login/upload/export.
10. MCP-ready — stable IDs/slugs, JSON exports with relationships, AI-readable Markdown.

---

## Local development

```bash
# 1. Clone & enter
git clone <your-mirror> severino-hq
cd severino-hq

# 2. Virtualenv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Environment
cp .env.example .env
# (for dev you can leave DEBUG=0 with a real SECRET_KEY, or set DEBUG=1)

# 4. DB + first user
python manage.py migrate
python manage.py createsuperuser

# 5. Optional demo data
python manage.py seed_demo

# 6. Run the dev server (bind to localhost only)
DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS=127.0.0.1 \
  python manage.py runserver 127.0.0.1:8000
```

Open <http://127.0.0.1:8000/>, sign in. Admin lives at `/admin/`.

### Importing a documentation manifest

Severino HQ does **not** read your Obsidian vault directly. Export a JSON
manifest from the vault (one entry per doc) and import it:

```bash
python manage.py import_docs_manifest path/to/docs_manifest.json
```

Or upload the file through the UI at **Docs → Import manifest**. See
`docs_index/importer.py` for the schema.

---

## Production deployment

Severino HQ is designed for **homelab / small VPS deployment, reachable only
over Tailscale**. Three documents cover this:

- [`docs/HOMELAB.md`](docs/HOMELAB.md) — the **concrete homelab deployment**
  running today (`/opt/apps/severino-hq` on `homelab-server`, NPM at
  `hq.jseverino.com`, read-only deploy key, AdGuard DNS rewrite). Read this
  first if you are redeploying our setup.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — generic deploy recipes:
  containerized (Docker Compose with named volumes for SQLite / receipts /
  exports, optional Tailscale sidecar) and systemd + Caddy/Nginx on a VPS.

Either way, the app binds to localhost (or the Tailscale interface), the
reverse proxy terminates TLS, and the public internet never sees it.

See [`docs/SECURITY.md`](docs/SECURITY.md) for the production security
checklist, and [`docs/BACKUP.md`](docs/BACKUP.md) for SQLite-safe backup &
restore with `VACUUM INTO` + `age` (or `restic`).

The roadmap — clients, invoices, the local `severino-knowledge-router` MCP,
the WordPress bridge, Postgres migration — is in
[`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## v1 quality bar

- Clean relational design (everything linkable: asset ↔ expense ↔ receipt ↔ project ↔ content ↔ doc).
- Authentication required on every URL except `/accounts/login/` and `/static/`.
- DEBUG off in production, SECRET_KEY from env, ALLOWED_HOSTS explicit, secure cookies.
- Uploaded receipts stored outside app code and served only through an auth-protected view.
- Audit logging on every CRUD action, login event, upload, and export.
- AI-readable Markdown export + relationship-aware JSON export, ready for the
  future local `severino-knowledge-router` MCP.
- Boring, reliable architecture. No SaaS dependencies.
