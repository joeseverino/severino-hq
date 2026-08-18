# Severino HQ — Claude Code guidance

Loaded automatically for every Claude Code session in this repo.

Read `AGENTS.md` first. It is the shared engineering contract, architecture
map, public/private boundary, quality bar, and definition of done. This file
adds Claude-specific operational guidance only.

---

## Deploy

**Landing on `main` IS the deploy.** Nothing is run by hand.

```
push/merge to main → ci (build + scan the host image)
                   → compose (host + every admitted extension → one image)
                   → deploy (self-hosted runner on the homelab, health-gated,
                             rolls back to the previous image on failure)
```

`compose` is triggered by `ci` completing successfully on `main`, never in
parallel with it. The image entrypoint re-runs `migrate` and `collectstatic`
on every boot, so there is no manual migration step.

**An extension merge deploys too, without anything being run by hand.** It
cannot signal the host directly — that would mean giving a private repository a
long-lived token that can start builds in a public one — so `compose` runs on a
schedule and rebuilds only when the composition's inputs actually changed:

```
merge to an extension → its own CI admits the wheel
                      → compose (scheduled, ≤15 min) notices new wheel digests
                      → deploy
```

Every run fingerprints `host image + wheel digests + admission policy` and
publishes it as a `composition:fp-…` tag. A scheduled run whose fingerprint is
already published stops before building, so the schedule costs one cheap
resolution per tick and deploys only when there is something to deploy. It also
self-heals: a trigger that is missed is picked up on the next tick.

To deploy an extension immediately rather than waiting, run the **Compose and
deploy extensions** workflow (`workflow_dispatch`) — a hand-run rebuild ignores
the fingerprint and always rebuilds.

If the user says "push it", "ship it", or "deploy it" after a code change:

1. `git commit` + `git push` (or open a PR and merge it — `main` is protected)
2. Watch the pipeline: `gh run watch`, or `gh run list --branch main --limit 3`
3. It is live when the `deploy` job in **Compose and deploy extensions** is
   green — not when `ci` is green, which has only built the host image

Step 1 alone is not "live", but neither is anything you type: what makes it
live is the pipeline finishing. Confirm the deploy job succeeded before
reporting a change as deployed.

### `hq deploy` is legacy. Do not run it.

It predates composition and was never updated for it. It resolves the last
green run of `ci.yml` and deploys `severino-hq:sha-…`, the **host-only**
image, while production runs `severino-hq/composition:…` — the host *plus*
every admitted extension. Running it takes the extensions off production —
whole domains stop existing. That is the exact failure composition was
introduced to end, so the command now does the thing its own architecture
forbids.

There is no situation in this repo where it is the right call. To rebuild the
current composed set by hand, run the **Compose and deploy extensions**
workflow (`workflow_dispatch`). To go back to a known-good release, re-run that
workflow at the commit you want.

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

- Django 6 + SQLite, server-rendered templates, plain CSS (no build step).
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
