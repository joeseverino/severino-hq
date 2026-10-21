# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.

## Setup

Install [mise](https://mise.jdx.dev) and a `python3`; `mise install` brings uv
with the other tools `fast` and `check` run. A tool only a heavier gate needs
(CodeQL, age) is `lazy` in `mise.toml`: its first use installs it, checked
against `mise.lock` like the rest.

```sh
git clone https://github.com/joeseverino/severino-hq.git
cd severino-hq
mise install
mise run dev:scratch --seed showcase
```

The log names the address and the throwaway account to sign in as. Nothing
reads a `.env` file: production's configuration is in
[Deployment](Deployment.md#configuration).

## Tasks

Every gate and server is a mise task. These are the ones to start from;
`mise tasks` lists every task with its description, and `mise run <task>
--help` shows what it takes.

| Task | What it is |
| --- | --- |
| `mise run fast` | The inner loop: the quick gates and the tests the change reaches. |
| `mise run check` | Does my change work: every Checks gate, the Tests gates, the composed suite. |
| `mise run ci` | Every gate the pipeline runs. |
| `mise run dev:scratch` | A throwaway server on loopback with a database of this checkout's own. |
| `mise run regen` / `mise run drift` | Rewrite every derived file from its source / check that none is behind. |
| `mise run preflight` | Ready to release: the gates, the composed suite and the deploy host. |

## Development servers

Each server is a mise task, and its env says where it listens and which
database it opens; `tools/dev/dev.sh` refuses what env cannot express. Every one
collects versioned assets and runs Uvicorn with reload on the production ASGI
path, watching this checkout and each extension's sources. The extension set
is the one `mise.local.toml` names ([The extension set](#the-extension-set)).
`dev:scratch` is the throwaway one; `dev` is the long-lived server behind a TLS
proxy, configured by the gitignored `dev.local.toml` (copy
`tools/dev/dev.local.example.toml`).

Only `dev` reads `dev.local.toml`, and a new worktree does not copy it, so the
others never take its bind or database. Each checkout has its own scratch
database, so servers in different worktrees run side by side; a second server
on one database is refused. `dev` refuses a linked worktree and a bind past
loopback and the tailnet range; scratch refuses anything but loopback.

Every loopback server shares the browser's cookies for `127.0.0.1` whatever the
port, so signing in to a second one signs the first out: open the second as
`http://localhost:<port>`.

Python dependencies come from `uv.lock`: `mise.toml` sets `UV_LOCKED`, so every
task's `uv run` refuses a lock that `pyproject.toml` has moved past, and no
virtualenv is made by hand. `uv lock` and `uv add` honor it too: change the
lock outside a mise task, or with `UV_LOCKED=0`. The default groups are `dev` and
`tools`; the browser gate adds `browser`. The image installs a hashed export
with every default group omitted.

## The gates

Every gate is a task named `<job>:<gate>` (`mise run checks:ruff` runs one),
and each CI job runs the aggregate of its name. `mise run check` answers "does
my change work?"; `mise run ci` answers "will the pipeline accept it?" and
keeps going past a failure to name every gate that failed. `fast` and `check`
end on a verdict line (`tools/gate/verdict.py`) naming each failed task and
test. The suite runs one worker per core (per performance core on Apple
silicon); `DJANGO_TEST_PROCESSES` overrides it, and `mise run fast --parallel 1`
rules parallelism out when a failure looks order- or isolation-dependent.

### What `fast` runs

`mise run fast [BASE]` maps the changed files against the merge base (`BASE`,
`$CHECK_BASE`, then `origin/main`, `main`) through the import graph to the
test modules that import them directly or through one module between, plus
every test of the changed Django apps, every test that names one (its URL
namespace or app label), and the architecture tests. Beside them it runs
`checks:ruff`, `checks:mypy`, `checks:structural`, `tests:django`, `tests:migrations` and every
`drift:*` gate. Each Django gate opens a database of its own, and its tests run
with `DEBUG` on, the one pass that does. A change to what extensions render
through (`templates/`, `static/`, `hq_sdk/`, the shared table, page, UI and
moment modules, template tags) also runs the extensions' suites, composed, when
`mise.local.toml` names a set. `--list` prints the selection.

### What `check` and `ci` run

`mise run check` runs every `checks:*` gate, the `tests:*` gates on the default
Python (the suite as production configures it, with `DJANGO_DEBUG` unset and
the coverage floor) and the composed suite, one task at a time so no two suites
share the machine's memory. A setting whose default follows `DEBUG` is held by
a test that loads the settings both ways.

`mise run ci` adds `tests` on every Python in `PYTHON_VERSIONS`, `browser`,
`controller`, `scan` (CodeQL) and `image`: the image build and what an image
must pass. The suite inside an image runs once, in Compose, on the composed
image production runs.

A test tagged `source` runs once, on the checkout, in `checks:source`
(`mise run checks:source <label>...` runs only the tagged tests those labels
name). A whole-suite run leaves the tag out; a run that asks for a tag no test
carries fails.

### The extension set

Name it once in a gitignored `mise.local.toml` (copy
`tools/dev/mise.local.example.toml`): `HQ_LOCAL_PYTHONPATH` says where the
extensions' sources are and `HQ_LOCAL_PLUGINS` which to enable. Only the
composed suite and the dev servers load the set. The composed suite,
`mise run suite:as-image`, runs as the image does: in a copy of only the files
`Dockerfile.dockerignore` admits, with `DEBUG` off and `SEVERINO_HQ_SOURCE`
stamped, so a test that reads a file the image leaves out fails here. Without
the set it runs the host alone and says so; `REQUIRE_COMPOSED=1` fails
instead, which is how `mise run preflight` runs it.

### Heavy gates take turns

Two suites at once exhaust a small machine's memory, so the heavy tasks (`fast`,
`check`, `ci`, `checks`, `tests`, `preflight`, the suites, the browser gates,
`demo:build`, `dev:look`, `docs:screenshot`) run under `tools/lib/heavy.sh`,
which holds an exclusive lock on `~/.cache/severino/heavy.lock` (under
`XDG_CACHE_HOME` when it is set) for as long as the task runs. Every checkout
and worktree shares that file. A heavy task started while another holds it
prints one line naming the holder (its pid, its mise task, how long it has run
and its command) and waits for the turn. The wait is bounded by
`SEVERINO_HEAVY_WAIT` seconds, two hours by default, the longest a heavy task
may run; past that it exits 75 (`EX_TEMPFAIL`) naming the holder again. A
heavy task run by one that holds the lock (`ci`
running `browser`, `check` running `suite:as-image`) runs inside its turn. The
lock goes with the process, so a killed gate frees it. CI runs one job per
machine and takes no lock.

A TOML task takes the turn by extending the `heavy` template in `mise.toml`,
or `django-heavy` when it is a Django gate; a file task sources
`tools/lib/heavy-turn.sh` and sets `#MISE timeout`. Each heavy
task has a timeout (30 minutes for a gate, two hours for an aggregate), so a
hung gate stops and frees the turn.

To see who holds it without starting anything:

```sh
lsof ~/.cache/severino/heavy.lock
```

### What the gates need

mise supplies the pinned tools; [Dependencies](Dependencies.md) says where
each pin lives. Beyond those:

- **An unprivileged account and a checkout it can write.** The systemd unit
  contracts model the host's runner with the current account, which must not
  be root. The readiness tests need `var/` writable.
- **Every supported Python** (`PYTHON_VERSIONS` in
  [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env)). `mise run tests` walks
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
off and on, and checks that nothing escapes the page sideways, a table fits
its region at every width, nothing moves once the server's page is drawn,
grid rows end together, siblings never overlap, and the rules of
`hq/platform/core/browser_audit.js` hold. A failure saves a screenshot to the OS
temporary directory. A check selects markup only through `SELECTORS`, and
`hq/platform/core/tests/test_browser_selectors.py` fails on a selector no
template renders.

It runs those checks on two estates.
The stress estate (`build_estate()` in
`hq/platform/core/tests/test_browser_fixtures.py`) breaks things on purpose:
long names, many rows, every state at once, on the pages it was built for.
Every list page is also filled from its own model
(`hq/platform/core/tests/test_browser_derived_fixtures.py`): each view drawn
through `TableListMixin` is found in the URLconf, and its rows carry every
field at its length, money at the largest a household records, and the
longest label of every choice, so a new list or field is measured at its
worst the day it exists (`dev:look derived/<app>/<name>` shows one).
The example estate (`hq/platform/core/example_estate/`, the homelab the demo
and `seed_demo` show) is read by `hq.platform.core.estate_browser_tests` on
every page the navigation declares, with script on and deferred parts loaded,
at 402, 820, 1360 and 1680 pixels, in the light palette and the dark. A
finding names its check, page, width and theme.
`HQ_BROWSER_ENGINE=chromium` or `firefox` runs both in another engine. One
browser test runs as `mise run manage -- test <label>` once
`mise run setup:webkit` has installed the engine.

The example estate's values have the shapes and lengths real providers give
(both tailnet addresses and a MagicDNS name per device, long image tags, a
zone with many records), so a layout that holds there holds on real data.
Each is invented or shared by every network:
`hq/platform/core/tests/test_example_estate_values.py` fails on an address or
host name outside the reserved and shared ranges it names.

`mise run dev:look -- <pages>` is the same browser for looking rather than
gating: it saves a full-page screenshot of each page at each width under
`var/look/`. `estate` names the example estate's pages; `--deep` adds every
page they link to (each record, each command form), answered by HQ's views as
the browser asks, every request marked as a prefetch so a page that would act
or reach out is refused and reported instead. `--rule survey` runs every
check the example estate's gate holds, and the ones a reader would otherwise
catch by eye: a word of running text split across lines, anything past the
content's gutter, text over text, a column empty in every row. `mise run dev:look --deep --rule survey estate/all` walks
the whole estate. What a rule finds on a page is printed and written beside
its screenshot (`<page>-<width>.txt`) as each page is looked at, so a run
stopped part way keeps what it found.

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
tools/                    development and CI programs, never in the image (tools/README.md)
scripts/                  what an outside contract calls by path: the deploy host's shims, the extension check
mise-tasks/               tasks whose body is a script, declared in its own #MISE header
tests/                    synthetic extension fixtures, the SDK consumer, property and shell suites
var/                      ignored local runtime state
```

Tests and migrations sit beside the app they verify. App labels, URL
namespaces and capability names are independent of package paths:
`hq.domains.projects` owns the `projects` app label and its tables.

### Runtime state

Django never guesses a database or a signing key: with DEBUG or without, it
refuses to start without `SEVERINO_DATABASE_PATH` and `DJANGO_SECRET_KEY`.
Every mise task that runs Django sets both (each gate a new empty database,
the scratch tasks this checkout's `var/dev/scratch/`), and the image names its
own, so `manage.py` runs as `mise run manage -- <command>` or
`mise run dev:manage -- <command>`. Media, exports and static files default to
`var/media`, `var/exports` and `var/static`; `SEVERINO_MEDIA_ROOT`,
`SEVERINO_EXPORTS_ROOT` and `DJANGO_STATIC_ROOT` override them. To move a
database, stop the application, back it up with its WAL contents, and check
the copy before pointing HQ at it.

### Extension environments

`uv sync` manages the host environment exactly, so never run it over an
environment that holds extension wheels: it removes what the host lock does
not name. Export runtime dependencies with `uv export --locked
--no-default-groups --no-emit-project` and install the hashed export
additively instead. The composed suite installs nothing; it imports the
extensions' sources through `PYTHONPATH`.
