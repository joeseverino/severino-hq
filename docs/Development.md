# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.

## Setup

Install [mise](https://mise.jdx.dev) and uv. mise reads the uv pin from
`pyproject.toml`, so it also needs a `python3` on the PATH.

```sh
git clone https://github.com/joeseverino/severino-hq.git
cd severino-hq
mise install
export DJANGO_DEBUG=1
uv run --locked python manage.py migrate
uv run --locked python manage.py createsuperuser
uv run --locked python manage.py seed_demo
mise run dev
```

Open <http://127.0.0.1:8000/> and sign in. `seed_demo` is optional: it fills
the database with the example estate, the same homelab the public demo shows
(`hq/platform/core/example_estate/`), and refuses a database that already holds
records rather than merging into it.
`mise run dev` collects versioned assets and runs Uvicorn with reload, on the
same ASGI path as production, so local browser checks exercise compression,
cache headers and routing. With `DJANGO_DEBUG` off, settings refuse to load
without a real `DJANGO_SECRET_KEY`, so local commands turn it on;
`mise run dev` sets it itself. Nothing reads a `.env` file: `.env.example` is
the production template, rendered by the host and named by
`SEVERINO_APP_ENV_PATH` ([Deployment](Deployment.md)).

Python dependencies come from `uv.lock` through `uv run --locked`, which every
task uses; no virtualenv is made by hand. The default groups are `dev` and
`tools`; the browser gate adds `browser`. The image installs a hashed export
with every default group omitted.

## The gates

| Command | Answers |
| :--- | :--- |
| `mise run fast [BASE]` | the inner loop: only what the change reaches, by import graph (`--list` prints the selection) |
| `mise run check` | do my changes work? Every Checks gate, the Tests gates on one Python, and the suite composed with the extensions, one at a time ([AGENTS.md](../AGENTS.md#start-here)) |
| `mise run ci` | will the pipeline accept them? Every gate CI runs, including CodeQL, Scorecard, the image and the browser gate |
| `mise run preflight` | is it ready? `ci`, the composed suite as the image runs it with the extensions required, and a read-only check of the deploy host |

Every gate is a task in [`mise.toml`](../mise.toml) named `<job>:<gate>`, and
each CI job runs the aggregate of its name, so the list exists once. `mise
tasks` prints it, `mise run checks:ruff` runs one gate, and `mise run -c ci`
keeps going past a failure and names every gate that failed. `mise run
preflight --remote` reads the checks GitHub ran on the pushed commit instead of
running `ci` locally. `--skip-host` runs the local gates only and exits 2.

The deploy host for preflight is `SEVERINO_HQ_DEPLOY_HOST`, an SSH
destination. The extension set for the composed suite is named in a gitignored
`mise.local.toml`, copied from
[`scripts/mise.local.example.toml`](../scripts/mise.local.example.toml):
`HQ_LOCAL_PYTHONPATH` (where the extensions' sources are) and
`HQ_LOCAL_PLUGINS` (which to enable). Without it the composed pass says it did
not run, and covers less. With it, `mise run fast` also runs the extensions'
suites when a change reaches what they render through.

Tests that read the repository (docs, workflows, the Dockerfile, the source)
carry `@tag("source")` and run once, in `checks:source`. A whole-suite run
leaves them out, so the image never runs a test against files it does not
ship. `mise run suite:as-image` runs the composed suite in a copy of only the
files the image is built from, configured as the image is; preflight runs it.
Derived files (the SDK contract, the API document and its clients, the
controller's contracts and Go types, `docs/Connections.md`) are never edited:
`mise run regen` rewrites every one from its source, and `mise run regen
--check` fails on drift. `mise run fast` runs the same drift checks.

The suite runs in parallel, one worker per core (per performance core on
Apple silicon; `DJANGO_TEST_PROCESSES` overrides it). `CHECK_PARALLEL=1 mise run fast` rules
parallelism out when a failure looks order-dependent; install `tblib` to see
the real error from a parallel worker.

### What the gates need

mise supplies the pinned tools; [Dependencies](Dependencies.md) says where
each pin lives. Beyond those:

- **An unprivileged account and a checkout it can write.** The systemd unit
  contracts model the host's runner with the current account, which must not
  be root. The readiness tests need `var/` writable.
- **Every supported Python** (`PYTHON_VERSIONS` in
  [`scripts/toolchain.env`](../scripts/toolchain.env)). `mise run tests` walks
  the list, each in its own `.venv-<version>`, with uv supplying the
  interpreter.
- **System tools:** Go at the version `controller/go.mod` names, and
  `ssh-keygen`, `sqlite3` and `zstd` for the shell suites.
- **A container runtime** for the `image` gates. CodeQL wants about 2 GB of
  memory.
- **Network, the first time:** mise downloads and verifies each tool.
  Scorecard's vulnerability check queries `api.osv.dev` on every run.

### Browser layout

`mise run browser` installs the WebKit the locked Playwright pins (HQ is read
in Safari) and runs `hq.platform.core.browser_tests` and
`hq.platform.core.estate_browser_tests`. It renders the
dashboard, service, connections, machine, topology and project pages over a
synthetic `example.*` estate at phone through desktop widths, with JavaScript
off, and checks that nothing escapes the page sideways, tables scroll in their
own container, grid rows end together, siblings never overlap, and the rules
of `scripts/layout-audit.js` hold. A failure saves a screenshot to the OS
temporary directory. A check selects markup only through `SELECTORS`, and
`hq/platform/core/tests/test_browser_selectors.py` fails on a selector no
template renders.

It runs those checks on two estates.
The stress estate (`build_estate()` in
`hq/platform/core/tests/test_browser_fixtures.py`) breaks things on purpose:
long names, many rows, every state at once, on the pages it was built for.
The example estate (`hq/platform/core/example_estate/`, the homelab the demo
and `seed_demo` show) is read by `hq.platform.core.estate_browser_tests` on
every page the navigation declares, with script on and deferred parts loaded,
at 402, 820, 1360 and 1680 pixels, in the light palette and the dark. A
finding names its check, page, width and theme.
`HQ_BROWSER_ENGINE=chromium` or `firefox` runs both in another engine.

### Debug toolbar

The Django Debug Toolbar is in the `dev` group and never in the image. It is on
when `DJANGO_DEBUG` is on and `SEVERINO_DEBUG_TOOLBAR=1`:

```sh
SEVERINO_DEBUG_TOOLBAR=1 mise run dev
```

It shows to loopback clients, or the addresses in
`SEVERINO_DEBUG_TOOLBAR_IPS`. While it is on, the two Trusted Types
directives are dropped from the CSP, because its panels use `innerHTML`
(`hq/config/devtools.py`).

## Repository layout

HQ is an unpackaged Django application managed by uv; the Go controller is
its own module.

```text
hq/
  config/                 settings, URLs and ASGI/WSGI entrypoints
  platform/
    application/          use cases, authorization and integration contracts
    core/                 platform persistence, rendering and authentication
    api/                  HTTP adapter and the derived OpenAPI description
    mcp/                  MCP adapter
    search_index/         shared search persistence
  domains/                domain apps, models, declarations and migrations
hq_sdk/                   the supported Python surface for extensions
controller/               Go: the controller, hq-host and hq-secrets
templates/  static/       shared and domain templates, authored assets
deploy/                   systemd units, tmpfiles rule, remote targets, host.json shape
composition/  policy/     the composed image and the extension admission policy
scripts/                  development, verification and deployment programs
tests/                    synthetic extension fixtures and property tests
var/                      ignored local runtime state
```

Tests and migrations sit beside the app they verify. App labels, URL
namespaces and capability names are independent of package paths:
`hq.domains.projects` owns the `projects` app label and its tables.

### Runtime state

Local defaults are `var/db`, `var/media`, `var/exports` and `var/static`;
`SEVERINO_DATABASE_PATH`, `SEVERINO_MEDIA_ROOT` and `SEVERINO_EXPORTS_ROOT`
override them. To use a database elsewhere, set `SEVERINO_DATABASE_PATH` to
it. To move one, stop the application, back it up with its WAL contents, and
check the copy before pointing HQ at it.

### Extension environments

`uv sync` manages the host environment exactly, so never run it over an
environment that holds extension wheels: it removes what the host lock does
not name. Export runtime dependencies with `uv export --locked
--no-default-groups --no-emit-project` and install the hashed export
additively instead. The composed suite installs nothing; it imports the
extensions' sources through `PYTHONPATH`.
