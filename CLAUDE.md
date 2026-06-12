# Severino HQ — Claude Code guidance

Loaded automatically for every Claude Code session in this repo.

---

## Deploy

**Pushing to GitHub does NOT update the running app.** The container on
`homelab-server` keeps running the previously deployed image until someone
redeploys it.

Routine deploy (after a `git push` from this Mac):

```bash
hq deploy            # rebuild + recreate the container
hq deploy --no-build # env / compose changes only, no rebuild
```

`hq deploy` runs `git pull --ff-only && docker compose up -d --build` on
`homelab-server`. The image entrypoint re-runs `migrate` and `collectstatic`
on every boot, so no manual migration step is needed.

If the user says "push it", "ship it", "deploy it", or similar after a code
change, the expected sequence is:

1. `git commit` + `git push`
2. `hq deploy`

Doing only step 1 is not "live." Confirm step 2 happened (or run it) before
reporting the change as deployed.

**First-time bringup** is a different procedure — see the vault runbook
[[Deploy Severino HQ]] (`rb-deploy-severino-hq`), reachable via the
`severino-vault-mcp` MCP.

## Frontmatter schema is shared with the MCP — don't hand-edit it

The frontmatter enum contract (doc_type / environment / status / sensitivity
values, doc_id prefixes) is defined **once**, in the MCP's `schema.py`. HQ
consumes it as `docs_index/schema.json`, which is **generated, not authored**:

```bash
hq schema            # regenerate docs_index/schema.json from the installed MCP
hq schema --check    # verify it's current (exit 1 on drift) — CI / pre-deploy
```

Rules so the single source can't drift:
- Never hand-edit `docs_index/schema.json`. Change `schema.py` in the MCP,
  reinstall (`site reinstall-mcp`), then `hq schema`, then commit + deploy.
- The manifest importer (`docs_index/importer.py`) validates against
  `docs_index/frontmatter_schema.py` (the committed JSON), **not** model
  `.choices`. Keep it that way — model choices would reintroduce drift.
- `DocumentationRecord`'s `TextChoices` stay (HQ's symbolic API + admin labels)
  but are guarded: `docs_index/tests.py` fails if their values diverge from the
  schema, or if the committed JSON lags the installed MCP. Run
  `manage.py test docs_index` on the dev Mac (where the MCP CLI lives) to
  enforce both.

## Operational questions

For anything about TLS, certs, DNS, NPM, Docker, Tailscale, AdGuard, the
homelab, or any "how do I X" on Joe's stack, use the `severino-vault-mcp`
MCP **first**:

1. `find_runbook("…")` (or `lookup_system` / `search_body`)
2. `read_doc(top_hit.doc_id)`
3. Answer in the doc's words.

Do not generate a generic tutorial when a runbook exists. See the user-global
`~/.claude/CLAUDE.md` for the full rule set.

## Stack quick map

- Django 5 + SQLite, server-rendered templates, plain CSS (no build step).
- App config: `config/settings.py`, `config/urls.py`.
- Domain apps: `core/`, `projects/`, `content/`, `docs_index/`, `assets/`,
  `expenses/`, `receipts/`, `reports/`.
- Templates: `templates/<app>/`; base layout `templates/base.html`;
  shared partials in `templates/partials/`.
- Styles: single file at `static/css/app.css`.
- Audit log: every create/update/delete/login/upload/export flows through
  `core/audit.py` → `AuditLog`.

## Running locally

No local venv is checked in. Either use Docker (`docker compose up`) or set
up a venv per the README. `manage.py check` won't run from the host
unless you've installed Django locally.

## Conventions

- Commit messages: terse `<area>: <what>` style (see `git log`).
- No `Co-Authored-By: Claude` trailers on commits. Solo-authored repo.
- No inline `style="…"` in templates — add a class to `app.css` instead.
- List-page tables must be wrapped in `<div class="table-scroll">` so they
  scroll horizontally on mobile instead of widening the page.
- Detail views with `{% if rel.all %}` + `{% for x in rel.all %}` panels
  need `prefetch_related` for those relations on the view's `queryset`.
