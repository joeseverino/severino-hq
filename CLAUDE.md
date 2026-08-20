# Severino HQ — Claude Code guidance

Loaded automatically for every Claude Code session in this repo.

Read `AGENTS.md` first. It is the shared engineering contract, architecture
map, public/private boundary, quality bar, and definition of done. This file
adds Claude-specific operational guidance only.

---

## Deploy

**Landing on `main` IS the deploy.** Nothing is run by hand.

| I changed | Do this | It is live when |
| --- | --- | --- |
| the host (this repo) | merge to `main` | the `deploy` job of **Compose and deploy extensions** is green |
| an extension (private repo) | merge it there | same job, within ~15 min |
| nothing — I want a rebuild | run **Compose and deploy extensions** (`workflow_dispatch`) | same job |

```
host merge      → ci (build + scan host image) → compose → deploy
extension merge → its CI admits the wheel      → compose (scheduled) → deploy
```

Five rules that cover every case:

1. **`ci` green is not live.** It has only built the host image. Production runs
   the *composed* image. Only the `deploy` job means live.
2. **Extensions cannot trigger this repo.** That would need a private repo to
   hold a token that starts builds in a public one. The schedule is the path.
3. **compose rebuilds only when inputs change** — host image, wheel digests,
   admission policy, hashed into a `composition:fp-…` tag. A hand-run rebuild
   ignores that and always rebuilds.
4. **Missed triggers self-heal** on the next tick. Never a stuck release.
5. **No manual migration.** The entrypoint re-runs `migrate` and
   `collectstatic` on every boot.

If the user says "push it" / "ship it" / "deploy it": commit + push (or PR and
merge — `main` is protected), then `gh run watch`. Confirm the `deploy` job
succeeded before reporting anything as deployed.

### When a deploy looks broken, check in this order

| Symptom | Cause | Fix |
| --- | --- | --- |
| Extension CI: `ModuleNotFoundError` for something the host has | the extension is pinned to an older host commit | there should be **no `HQ_COMMIT` variable** — `hq-ref` falls back to `main`. Deleting it is the fix, not bumping it |
| compose sits in "Resolve the host image", then fails | the host `ci` for that commit failed, so the image was never published | fix the host build. compose checks the host's conclusion every 2 min and stops early, naming it |
| Merged an extension, nothing deployed | waiting for the next scheduled tick | wait ≤15 min, or run compose by hand |
| `Deploy composition` skipped | nothing was published — a PR, or a scheduled run whose fingerprint already exists | expected, not a failure |
| `security` job fails on a dependency you didn't touch | a new advisory published against a pinned transitive dep | `uv pip compile requirements.in --output-file requirements.txt --generate-hashes --python-version 3.12 --universal --upgrade-package <name>` |

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

## Frontmatter schema is shared with the vault engine — don't hand-edit it

The frontmatter enum contract (doc_type / environment / status / sensitivity
values, doc_id prefixes) is defined **once**, in `vault-engine`:
`~/Documents/Code/Assets/vault-engine/src/vault_engine/schema.py`, which holds
`LABS_PROFILE` and `EDUCATION_PROFILE`. The vault and edu MCPs are thin servers
over that package — `severino-vault-mcp` has no `schema.py` of its own; its
`tests/test_schema_contract.py` imports `from vault_engine.schema import
LABS_PROFILE`. HQ consumes the contract as `docs_index/schema.json`, which is
**generated, not authored**:

```bash
hq schema            # regenerate docs_index/schema.json via `svmc schema --json`
hq schema --check    # verify it's current (exit 1 on drift) — CI / pre-deploy
```

Rules so the single source can't drift:
- Never hand-edit `docs_index/schema.json`. Change `schema.py` in **vault-engine**,
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

## The host does not name its extensions

See the section of the same name in `AGENTS.md` for why: a host that names an
extension has taken a dependency on it, and the point of this design is that it
has none. Everything here is part of the published artifact — code, comments,
commit messages, PR prose, workflows, docs — so the constraint applies to prose
as much as to imports. Write examples as `example_notes` or `<extension>.<work>`.

`application/test_plugins.py` enforces it in the composed image, where the real
extension set exists: one test keeps `composition/extensions.json` empty, and
one walks the source tree for any installed extension's id, distribution, app or
urlconf package. Both take their terms from runtime composition rather than from
anything committed. Public CI cannot run them — it has no extensions — so run
the composed pass locally when a change touches `hq_sdk`:

```bash
CHECK_PYTHON=.venv/bin/python PYTHONPATH=… SEVERINO_HQ_PLUGINS=… ./scripts/check.sh
```

`hq dev` computes those two values. Fix a coupling before pushing rather than
after: once published, a force-push does not unpublish.

## Conventions

- Commit messages: terse `<area>: <what>` style (see `git log`).
- No `Co-Authored-By: Claude` trailers on commits. Solo-authored repo.
- No inline `style="…"` in templates — add a class to `app.css` instead. The
  sole exception is a per-datum CSS custom property on a chart mark
  (`style="--at: 62%"`), which a class cannot express; `core/tests.py` pins
  `style-src` as the only CSP directive allowed to relax.
- List-page tables must be wrapped in `<div class="table-scroll">` so they
  scroll horizontally on mobile instead of widening the page.
- Detail views with `{% if rel.all %}` + `{% for x in rel.all %}` panels
  need `prefetch_related` for those relations on the view's `queryset`.
