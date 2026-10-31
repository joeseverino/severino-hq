# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.
How agents work on HQ through these gates: [AI](AI.md).

## Setup

Install [mise](https://mise.jdx.dev) and a `python3`; `mise install` brings uv
with the other tools `fast` and `check` run. A tool only a heavier gate needs
(CodeQL, age) is `lazy` in `mise.toml`: its first use installs it, checked
against `mise.lock` like the rest. Go is the one tool `mise.toml` does not
pin itself: it reads the release from `controller/go.mod`'s `toolchain` line,
which Renovate moves, so mise loads `mise.toml` only beside that file.

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

Every gate and server is a mise task: which to start from, and how to list
the rest, is in [AGENTS.md](../AGENTS.md#start-here). `mise doctor project`
says whether this checkout can compose its extensions, with the fix for each
gap.

`mise run test --at <ref> <label>...` runs this tree's test files (every
`tests/` directory and the browser gates' modules, uncommitted work included)
on `<ref>`'s code: does a new test fail on the old code, was a failure already
there. `mise tasks info test` says what `--keep` holds back, and the closing
line what was laid over and kept, or that a label does not import at the ref.

The `audit:` tasks measure without writing, so a read-only auditor may run
any of them: `audit:ruff <rules> [paths]...` counts Ruff's findings per rule,
`audit:ast [globs]...` counts per file the docstrings over 8 lines and the
`getattr` calls of a literal name with a default (ast-grep rules in
`tools/audit/ast-grep.yml`), `audit:imports <path>` names a file's importers
and `audit:go [linters]` counts golangci-lint's findings. A path or glob
narrows the count, and `!glob` leaves files out, so
`mise run audit:ruff SLF001 '!**/tests/**'` and
`mise run audit:ruff SLF001 '**/tests/**'` count source and tests apart.

## Development servers

Each server is a mise task, and its env says where it listens and which
database it opens; `tools/dev/dev.sh` refuses what env cannot express. Every one
collects versioned assets and runs Uvicorn with reload on the production ASGI
path, watching this checkout and each extension's sources through watchfiles
(the `dev` group): a Python file or a template restarts it, and nothing under
`.claude/worktrees`, `.venv` or `var/` does, so another worktree's edits leave
it running. `dev:scratch --no-reload` serves without the reloader, one process
instead of three, for a server that only needs to answer. The extension set
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

Every gate is a task named `<job>:<gate>` (`mise run lint:ruff` runs one),
and each CI job is named for the task it runs: `lint`, `test`, `browser`,
`build` and `scan`, each reaching its job's gates through a wildcard
(`GateReferenceTests` holds every job id to its task). A gate is declared in its job's
file in `mise-tasks/` (`mise-tasks/lint.toml` holds `lint:*`), or beside it
in `mise-tasks/lint/` when its body is a script; mise finds both there by
default, and `mise.toml` keeps the tools, the environment, the templates, the
development servers and the `lint` and `scan` tasks. A task outside `mise.toml` names no
file in its env: mise reads an env path, and renders `config_root` inside env, from
the directory of the file that declares the task (`GateReferenceTests`).
On a pull request CI's **Ready** job needs every other job and fails when one
did not pass: it is the one check the `main` ruleset requires, so a gate added
to a job never touches the repository's settings (`VerdictNeedsTests`).
`mise run check` answers "will the pipeline accept it?" and
keeps going past a failure to name every gate that failed. `fast`, `check`,
`lint`, `test`, `browser`, the composed suite and an extension's check
(`tools/pipeline/check-plugin.sh`) end on a verdict line
(`tools/gate/verdict.py`) naming each failed leaf task and test and the one
command that reruns just those, and print where the whole verdict is kept, in
`var/gate/` beside the run before it, with each failed test's exception and
traceback and each failed task's last lines. The suite runs one worker per core (per performance core on Apple
silicon); `DJANGO_TEST_PROCESSES` overrides it, and `mise run fast --parallel 1`
rules parallelism out when a failure looks order- or isolation-dependent.

### What `fast` runs

On an 8 GB Apple silicon machine, `mise run test <label>` takes a few
seconds, `fast` one to two minutes, `lint` about one, and `lint:prose`
about five seconds.

`mise run fast [BASE]` maps the changed files against the merge base (`BASE`,
`$CHECK_BASE`, then `origin/main`, `main`) through the import graph to the test
modules that import them directly or through one module between, plus every
test of the changed Django apps, every test that names one (its URL namespace
or app label), every test that reads the tasks when a task definition or a file
a task's run names changed, and the architecture tests. A change to a module
under `hq/` or `hq_sdk/` adds every test tagged `tree` to the same run: such a
test finds its files by walking this checkout, which no import shows
(`test_tree_tag.py` holds the tag to the walks; a test that patches the `ROOT`
of the code it drives walks a fixture and carries none). Beside them it runs
each quick gate whose paths the change touches, as `TASKS` in
`tools/gate/fast.py` lists them; a change to the dependencies, tool pins or
task definitions runs them all. Each Django gate opens a database of its own,
and its tests run with `DEBUG` on, the one pass that does. A change to what
extensions render through (`templates/`, `static/`, `hq_sdk/`, the shared
table, page, UI and moment modules, template tags) also runs the extensions'
suites, composed, when `mise.local.toml` names a set. `--list` prints the
selection.

### What `check` runs

`mise run check` runs what CI's gate jobs run, one task at a time so no two
suites share the machine's memory: `lint` (every `lint:*` gate, the Go
controller's static half and the derived files among them), `test` (the
`test:*` gates on every Python in `PYTHON_VERSIONS`, the suite as production
configures it with `DJANGO_DEBUG` unset and the coverage floor, then the Go
controller's race tests once), `browser`, `build` (the image build and what an
image must pass) and `scan` (CodeQL). A setting whose default follows `DEBUG`
is held by a test that loads the settings both ways. The suite inside an image
runs once, in Compose, on the composed image production runs.

`mise run check --release` then runs the composed suite with the extensions
required and checks the deploy host, read-only, on the committed `HEAD`: exit 0
means ready to release, and its verdict is kept apart, in
`var/gate/check-release.txt`. `--remote` reads every check GitHub ran on the
pushed commit instead of running the gates here, and `--skip-host` leaves the
host out and exits 2, never 0 ([Deployment](Deployment.md)).

A test tagged `source` runs once, on the checkout, in `lint:source`
(`mise run lint:source <label>...` runs only the tagged tests those labels
name). A whole-suite run leaves the tag out; a run that asks for a tag no test
carries fails.

The wording rules are Vale's, in `tools/lint/vale`: `.vale.ini` there says
which files each style reads and `styles` holds the rules, and `lint:prose`
runs them, with `hq/platform/core/plain_words.py` handing Vale the template
attributes and tag literals it cannot read. A finding names its rule and what to say
instead; a word that must stand in one file is turned off for that file in
that `.vale.ini`, with the reason beside it.

### Derived files

`mise run fix` rewrites every file generated from a source, in the order
each reads the last: `hq_sdk/contract.json` with the SDK's text examples and
the table in `docs/Plugins.md`, the machine API's OpenAPI document, the
controller's contracts from the connection registry and the Go types generated
from them, `docs/Connections.md` from what the providers declare, and the
served font cut to the characters HQ prints, then formats as the lint gates
read. Each `lint:drift-*` gate checks one against its source without writing,
and `fast` runs the ones a change reaches. A rebase conflict in one is settled
by `mise run fix`, never by hand.

The OpenAPI document's examples are responses the API's tests received,
recorded in `hq/platform/api/openapi-examples.json`. A recording is a serial
run of those tests under a clock that stands still, with fixed ids, so it reads
the same every run: `lint:drift-api-examples` records one beside the file and
fails when they differ, and `mise run fix:api-examples` records them again,
then rewrites the document. It is not a step of `fix`, being a run of the suite.

### The extension set

`mise run setup:extensions <checkout>...`, run once per machine in the main
checkout, names it in the gitignored `mise.local.toml`, reading each package
of each checkout as admission reads it: `HQ_LOCAL_PYTHONPATH` lists their
`src` directories and `HQ_LOCAL_PLUGINS` the manifests they declare. mise
writes the file and keeps every other value in it, and `mise doctor project`
then checks that the set imports. A worktree Claude Code makes gets a copy of
the file through the gitignored `.worktreeinclude`, so it composes the
extensions' main checkouts. A `HQ_LOCAL_PYTHONPATH` already in the environment
wins, so an extension worktree is composed by naming its sources on the
command line:
`HQ_LOCAL_PYTHONPATH=<worktree>/packages/<name>/src mise run test --composed`,
one `src` per package, joined by `:`. Only the composed suite, the dev
servers and `mise run dev:look --composed` load the set; the suite and the
look load it through `tools/lib/extension-set.sh`, whose first line names the
`src` each package is imported from, so a run says which tree it composed.

`mise run test --composed` runs the suite as the image does: in a copy of only
the files `Dockerfile.dockerignore` admits, less what the Dockerfile's
`COPY --exclude` keeps out of `/app`, with `DEBUG` off, `SEVERINO_HQ_SOURCE`
stamped and only the runtime dependencies (`.venv-image`). A test that reads a
file the image leaves out, or loads a development tool, fails here. Without a
set it runs the host alone and says so; with `REQUIRE_COMPOSED=1`, as
`mise run check --release` runs it, it fails instead.

The same file holds this machine's other values, each written with
`mise set --file mise.local.toml NAME=value`:

- `SEVERINO_HQ_DEPLOY_HOST`, the SSH destination `mise run check --release`
  checks read-only before a release.
- `SEVERINO_FAILURE_LOG_IDENTITY`, the age identity that opens a failed run's
  sealed logs (`mise run failure-logs`): a password-manager reference read at
  use, or a file path.

Compose builds each extension from its branch named like the host branch, or
from its admitted main when it has none, so a host branch pushed ahead of its
extension work is verified without it. `mise-tasks/git/pre-push` refuses that
push: with a set named, every extension checkout with work off main must carry
the host branch's name and be pushed first. The global pre-push hook runs it
with git's refs on stdin.

### Heavy gates take turns

Two suites at once exhaust a small machine's memory, so every task that loads
the machine (a suite, a browser, an image build, CodeQL, an aggregate of
them) waits for one turn shared by every checkout and worktree, and a waiting
task names the holder. `tools/lib/heavy.sh` says how the turn is held, waited
for and bounded, and `HeavyTaskTests` (`tools/gate/tests/test_turn.py`) holds
every such task to it. A TOML task takes it by extending the `heavy` template
in `mise.toml` (`django-heavy`, `django-browser`), a file task by sourcing
`tools/lib/heavy-turn.sh`. A run of tests is narrow, and goes ahead outside
the turn, when each label names a module or something in one and no test it
loads drives a browser (`tools/gate/turn.py`), so `mise run test <module>`
starts at once.

A heavy task declares `SEVERINO_HEAVY_BOUND` in its env, how long it may run
once its turn starts, and carries no mise `timeout`, whose clock runs while
the task waits: so heavy tasks asked together (`mise run browser:layout-1
::: browser:layout-2`) queue on the turn and each runs in full.

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
  [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env)). `mise run test` walks
  the list, the first in the project's `.venv` and each other in its own
  `.venv-<version>`, with uv supplying the interpreter.
- **System tools:** `ssh-keygen`, `sqlite3` and `zstd` for the shell suites.
- **A container runtime** for `build`'s gates. CodeQL wants about 2 GB of
  memory.
- **Network, the first time:** mise downloads and verifies each tool.
  Scorecard's vulnerability check queries `api.osv.dev` on every run.

### Browser layout

`mise run browser` installs the WebKit the locked Playwright pins (HQ is read
in Safari) and runs the package `hq.platform.core.tests.browser`. It renders the
dashboard, service, connections, machine, topology and project pages over a
synthetic `example.*` estate at phone through desktop widths, with JavaScript
off and on, and checks what a reader would lose: nothing escapes the page
sideways, a table fits its region at every width, no text runs out of its box
or over other text, nothing is out of reach, text and controls keep their
contrast, and the unwanted-scrollbar and clipped-content rules of
`hq/platform/core/browser_audit.js` hold. Its Chromium legs also hold each
page's Cumulative Layout Shift to 0.1. Taste (evenness, placement, row
height) is written in [Design](Design.md#held-by-eye) and judged by eye. A failed subtest saves a screenshot
of its page as it failed, at its width, named for its page, width and rule
(`example-two-402-overflow.png`), in a `hq-browser-failure-<test>-*` folder in
the OS temporary directory; a test that fails outside its subtests saves
`page.png` there. CI uploads every such folder of a failed gate. A check selects markup only through `SELECTORS`, and
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
and `manage.py seed showcase` show) is read by `hq.platform.core.tests.browser.estate` on
every page the navigation declares, with script on and deferred parts loaded,
at 402, 820, 1360 and 1680 pixels and at `WIDE_TABLES`, in the light palette
and the dark. `WIDE_TABLES` is read from the stylesheet: the narrowest
window where a table's region, on the page and in a card, has passed the
breakpoint its wide rules need, so the columns that keep their line are
measured with the least room; the stress estate is held there too. A
finding names its check, page, width and theme.
`HQ_BROWSER_ENGINE=chromium` or `firefox` runs both in another engine; CI
runs the layout gate in two legs side by side (`browser:layout-1`,
`browser:layout-2`) and runs them in Chromium as well
(`browser:layout-chromium-1`, `-2`), which `mise run browser` leaves to CI. One browser test runs as
`mise run test <label>` once `mise run setup:browser webkit` has installed the
engine, and `mise run browser <leg>...` runs the legs named (`layout-1`, `demo`).

A failure only CI's Linux shows is reproduced with `mise run browser --linux`
and a leg (`layout-chromium-1`) or test labels, drawn in Chromium
unless `--engine` names another. It runs them on this checkout inside the
Playwright image `ci.yml` gives the browser job, as linux/amd64 with the job's
options, set up as the toolchain action sets a runner up, and copies a failed
rule's screenshots to `var/browser-linux/`. It needs a container runtime that
runs x64 through Rosetta (`colima start --cpu 4 --memory 5 --vz-rosetta`).
Emulated, the browser draws slower than on a runner, so a rule that reads a
page against its frames fails more often than in CI: a whole leg's classes
side by side fail far past CI's list, and two classes named as labels come
closer to it.

Every page a gate serves runs under the policy HQ sends: a page rendered
through the test client keeps its response's headers (`gate.answered`), the
API reference its own derived policy, and a page a test puts together HQ's.
A policy refusal, a console error or an uncaught script error fails the
test, naming the page: a refusal its directive and what it refused, an
uncaught error the width, its message and the frame it was thrown from. The
example estate fails page by page, every other gate at the end of its test.

The gates wait for Inter, but a reader whose copy is late is drawn in
whatever face the platform has (`font-display: optional`), and each platform
sets text at its own widths. `--fallback-face` on a layout leg,
`browser:navigation` and `dev:look` (`HQ_BROWSER_FACE=fallback` for a
`mise run test` label) withholds Inter's face, so every page is drawn in the
next face of the stylesheet's stack (`--font`), the platform's system face,
and a layout that holds only at Inter's widths fails.

What the browser gates read from the environment:

| Variable | Set by | Does |
| :--- | :--- | :--- |
| `HQ_BROWSER_ENGINE` | development | the browser gates' engine: `webkit` by default, `chromium` or `firefox` |
| `HQ_BROWSER_CHANNEL` | development | a Playwright browser channel for that engine |
| `HQ_BROWSER_FACE` | development | `fallback` draws the browser gates with Inter withheld, in the stack's next face (`--fallback-face`) |

A test that stalls fails and says where. Each test process (every parallel
worker) watches for progress: a test, a class fixture or a subtest starting, a
subtest ending, a page in `dev:look`. Once none has come for
`HQ_TEST_STALL_SECONDS` (300 unless the environment says otherwise; `0` turns
it off, for a debugger), it prints a stall report: what it was running, every
thread's and greenlet's stack, and in a browser gate the end of Playwright's
own log (the browser's launch and output, each call and navigation). Then the
test or fixture fails with `Stalled` and the run goes on. One that does not
stop within 30 seconds of that ends its process, and a parallel worker that
ends stops the whole run, so a hang never holds the heavy turn or a CI job
until its bound.

The example estate's values have the shapes and lengths real providers give
(both tailnet addresses and a MagicDNS name per device, long image tags, a
zone with many records), so a layout that holds there holds on real data.
Each is invented or shared by every network:
`hq/platform/core/tests/test_example_estate_values.py` fails on an address or
host name outside the reserved and shared ranges it names.

`mise run dev:look <pages>...` is the same browser for looking rather than
gating; `mise run dev:look --help` gives its pages, its flags and three
commands to copy. It saves a full-page screenshot of each page at each width
under `var/look/`, which each run empties first, and a page taller than the
window also as window-sized bands (`<page>-<width>.band-01.png` on). A page
that could not be looked at (refused, no `main`, a rule that could not run) is
shot as it stands, as `<page>-<width>.failed.png`. An unknown page or rule is
refused before anything runs, with the nearest named, and a page that answers
anything but 200 fails the run. The look keeps its test database in
`var/dev-look/` through Django's `--keepdb`, so a second run creates nothing;
an edited migration needs `rm -r var/dev-look`.

`--composed` loads the extension set as the composed suite does
([The extension set](#the-extension-set)), so `estate/all`, a route name such
as `finance:home` and `--deep` reach the extensions' pages, and its first line
names the tree each is imported from. The extensions ship no data set of
their own, so their pages draw empty beside the seed's estate. A composed
look keeps its database in `var/dev-look/composed/`.

`--deep` asks every linked page marked as a prefetch, so a page that would act
or reach out is refused and reported instead. `--rule survey` runs every check
the gates hold (`BrowserGate.findings`), so what it finds on a page the gates
read is what they fail on; markup a vendored script draws
(`SELECTORS["vendored"]`, the API reference's viewer) is held by its own
tests instead. What a rule finds is written beside each screenshot
(`<page>-<width>.txt`) as the run goes, so a run stopped part way keeps it,
and to `var/gate/look.txt` once the run ends. Console errors, uncaught script
errors and policy refusals are judged as findings are: without a base any
fails the run, against one only a new one does.

`--base <ref>` looks at the same pages at `<ref>` first, in a temporary
worktree with this checkout's test files laid over it as `test --at` does,
and judges both by this checkout's rules. The base's screenshots are kept in
`var/look/base/`, under the same names as this checkout's in `var/look/`, so
the two sides of a change can be compared once the worktree is gone. Findings are matched as a multiset
by page, width, check and a key with every UUID, hex id and origin panel's id
taken out, each size and contrast kept to its power of four, and every other
number and the record named after a measure taken out, so one whose row
number moved, whose overflow grew by a few pixels, or that names another
record is neither new nor fixed, and one grown many times over is new. With
`--columns` or `--eval`, each line the base measured otherwise is printed
under this checkout's, marked with the ref.

`mise run perf <pages>... [--image] [--seed <name>] [--base <ref>]
[--host-alone] [--fresh] [--requests <n>]` measures how light and fast HQ is,
page by page: server time, queries, the rows its tables hold and its HTML
size, then bytes, requests, first and largest contentful paint (first visit
and repeat), script time and layout shift in Chromium, memory
and cold start. `pages` names the example estate's pages as `dev:look` does,
or `all` for every page its navigation lists; a page asked that answers
anything but 200 fails the run. `--seed` names the data set the servers are seeded
with, as `dev:scratch --seed` does (default showcase); the stress sets live in
the test tree, so no server can serve them. Each run is kept as
`var/perf/<run>.json`, saved after each measurement; a failed one is recorded
with its error and the rest go on, and each step says how long it took.
`<run>.txt`, which the session log shows, is the summary, and `<run>.table`
has every page.

`--base` keeps both servers up and asks every page of both in turn over
`--requests` rounds, alternating which goes first, before anything else
touches either server. Each number is shown beside how far it moved, and each
page's server time beside its median round-by-round difference and that
difference's interquartile range. A page whose difference is past that range
is marked moved, and the closing line counts those pages. Unless `--fresh`,
the base's browser, memory and cold start come from a complete kept report
under the same plan, seed included. A ref that refuses the extension set for
its plugin API makes the run measure the host alone on both sides, as
`--host-alone` asks, and its closing line says so. It is a report, not a
gate. With `--base`, one page takes about 40 s (under 30 with the base kept)
and every page about two and a half minutes (two with the base kept).
`mise run perf:note <label> <value> [unit]` keeps a measurement made by hand for the session log.

A look holds still: the page's clock is frozen at the moment the run began
(its `--base` run shares that instant), and each screenshot is taken with
animations finished. `--open` opens disclosures round after round until
nothing more opens, then shows one popover of each kind on its own, shot as
`<page>-<width>.popover-<id>.png`; a kind is the popover's classes and the
table and column it sits in, so a list's many panels from one partial cost
one run. With `--expand` an open row holds the columns at the widths they had
before it opened (`tables.js`), and `--columns` measures a held table's min
and max on a copy without the hold. `--dialog` presses a control that opens
the dialog, after any menu holding it, so the page's own wiring opens it.

`--eval` takes a file holding a function, called with nothing, or one
expression; what it returns prints under the page and width (as JSON unless
it is text) and is kept beside the screenshot as `.eval.txt`. A computed
style, or an element's chain of ancestors with their widths:

```js
() => {
  const chain = [];
  for (let el = document.querySelector('.table-scroll td'); el; el = el.parentElement) {
    chain.push(`${el.localName}.${[...el.classList].join('.')} ${Math.round(el.getBoundingClientRect().width)}`);
  }
  return chain;
}
```

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
mise-tasks/               every task outside mise.toml: one TOML file per CI job (lint.toml, ...) and operator.toml, and beside each a folder of script tasks
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
`mise run manage --scratch -- <command>`. Media, exports and static files default to
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
