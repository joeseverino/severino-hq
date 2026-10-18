# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.

## Setup

Install [mise](https://mise.jdx.dev), uv and a `python3`. uv is not a mise
tool: any uv works, since `uv.lock` decides what runs. The toolchain gate and
the structural bar run `python3` directly.

```sh
git clone https://github.com/joeseverino/severino-hq.git
cd severino-hq
mise install
mise run dev:scratch --seed showcase
```

The log names the address and the throwaway account to sign in as. Nothing
reads a `.env` file: `.env.example` is the production template, rendered by
the host and named by `SEVERINO_APP_ENV_PATH` ([Deployment](Deployment.md)).

## Development servers

Each server is a mise task, and its env says where it listens and which
database it opens; `scripts/dev.sh` refuses what env cannot express. Every one
collects versioned assets and runs Uvicorn with reload on the production ASGI
path, watching this checkout and each extension's sources. The extension set
is the one `mise.local.toml` names ([AGENTS.md](../AGENTS.md#start-here)).

| Task | What it is |
| --- | --- |
| `mise run dev:scratch` | A throwaway server: loopback, DEBUG on, a new database under this checkout's `var/dev/scratch/` each start, on a port the system picks. `--keep` reuses the last database, `--seed showcase` or `standard` fills it with the example estate (`seed_demo`), `--port` fixes the port, `--plan` prints the environment and exits. |
| `mise run dev:session` | Prints a session cookie (`dev_session`) for the scratch server's account, for a browser context or `curl --cookie`. |
| `mise run dev:manage -- <command>` | `manage.py` against the scratch database, extensions loaded. `routes [filter]` lists each named route and its path. |
| `mise run dev:demo [seed]` | Builds that demo seed and serves the static site on loopback ([demo](../demo/README.md)). |
| `mise run dev` | The long-lived server behind a TLS proxy: DEBUG off, single sign-on, its bind, hostnames and database read from the gitignored `dev.local.toml` (copy `scripts/dev.local.example.toml`). It reads its signing key and client secret from the password manager when it starts. `--fresh` deletes its database first; `--plan` prints the environment, without secrets, and exits. |

Only `dev` reads `dev.local.toml`, and a new worktree does not copy it, so the
others never take its bind or database. Each checkout has its own scratch
database, so servers in different worktrees run side by side; a second server
on one database is refused. `dev` refuses a linked worktree and a bind past
loopback and the tailnet range; scratch refuses anything but loopback.

Every loopback server shares the browser's cookies for `127.0.0.1` whatever the
port, so signing in to a second one signs the first out: open the second as
`http://localhost:<port>`.

Python dependencies come from `uv.lock` through `uv run --locked`, which every
task uses; no virtualenv is made by hand. The default groups are `dev` and
`tools`; the browser gate adds `browser`. The image installs a hashed export
with every default group omitted.

## The gates

The gates, what each answers, how to name the extension set and how to run one
gate alone are in [AGENTS.md](../AGENTS.md#start-here). The suite runs one
worker per core (per performance core on Apple silicon);
`DJANGO_TEST_PROCESSES` overrides it.

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
SEVERINO_DEBUG_TOOLBAR=1 mise run dev:scratch
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
composition/              the composed image
policy/                   the extension admission policy
demo/                     the static demo site built from HQ's own pages
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
