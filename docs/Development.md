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
on `<ref>`'s code: does a
new test fail on the old code, was a failure already there. `--keep` leaves a
support file (not `test*.py` and not named by a label) as the ref has it, for
a check whose logic lives in a gate, probe or helper. A label that does not
import at `<ref>` is its own verdict, `DOES NOT IMPORT`, exit 3: no test ran
on that code.

## Development servers

Each server is a mise task, and its env says where it listens and which
database it opens; `tools/dev/dev.sh` refuses what env cannot express. Every one
collects versioned assets and runs Uvicorn with reload on the production ASGI
path, watching this checkout and each extension's sources through watchfiles
(the `dev` group): a Python file or a template restarts it, and nothing under
`.claude/worktrees`, `.venv` or `var/` does, so another worktree's edits leave
it running. The extension set
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
and each CI job runs the aggregate of its name. A gate is declared in its job's
file in `mise-tasks/` (`mise-tasks/checks.toml` holds `checks:*`), or beside it
in `mise-tasks/checks/` when its body is a script; mise finds both there by
default, and `mise.toml` keeps the tools, the environment, the templates, the
development servers and the aggregates. A task outside `mise.toml` names no
file in its env: mise reads an env path, and renders `config_root` inside env, from
the directory of the file that declares the task (`GateReferenceTests`).
On a pull request CI's **Ready** job needs every other job and fails when one
did not pass: it is the one check the `main` ruleset requires, so a gate added
to a job never touches the repository's settings (`VerdictNeedsTests`).
`mise run check` answers "does
my change work?"; `mise run ci` answers "will the pipeline accept it?" and
keeps going past a failure to name every gate that failed. `fast` and `check`
end on a verdict line (`tools/gate/verdict.py`) naming each failed task, as
mise's `-c` summary names it, and each failed test, as the test runner writes
it into the folder `HQ_TEST_REPORTS` names, kept with every name in full in
`var/gate/<gate>.txt`, whose path the line above it prints; there each failed
test carries the exception it raised, and its traceback follows the list. The suite runs one worker per core (per performance core on Apple
silicon); `DJANGO_TEST_PROCESSES` overrides it, and `mise run fast --parallel 1`
rules parallelism out when a failure looks order- or isolation-dependent.

### What `fast` runs

`mise run fast [BASE]` maps the changed files against the merge base (`BASE`,
`$CHECK_BASE`, then `origin/main`, `main`) through the import graph to the
test modules that import them directly or through one module between, plus
every test of the changed Django apps, every test that names one (its URL
namespace or app label), every test that reads the tasks when a task
definition changed, and the architecture tests. Beside them it runs each quick
gate whose paths the change touches, as `TASKS` in `tools/gate/fast.py` lists
them; a change to the dependencies, tool pins or task definitions runs them
all. Each Django gate opens a database of its own, and its tests run
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

### Derived files

`mise run regen` rewrites every file generated from a source, in the order
each reads the last: `hq_sdk/contract.json` with the SDK's text examples and
the table in `docs/Plugins.md`, the machine API's OpenAPI document, the
controller's contracts from the connection registry and the Go types generated
from them, `docs/Connections.md` from what the providers declare, and the
served font cut to the characters HQ prints. `mise run drift` checks each against its source without writing, and
`fast` runs the ones a change reaches. A rebase conflict in one is settled by
`regen`, never by hand.

### The extension set

Name it once in a gitignored `mise.local.toml` (copy
`tools/dev/mise.local.example.toml`): `HQ_LOCAL_PYTHONPATH` says where the
extensions' sources are and `HQ_LOCAL_PLUGINS` which to enable. Only the
composed suite and the dev servers load the set. The composed suite,
`mise run suite:as-image`, runs as the image does: in a copy of only the files
`Dockerfile.dockerignore` admits, less what the Dockerfile's `COPY --exclude`
keeps out of `/app`, with `DEBUG` off and `SEVERINO_HQ_SOURCE`
stamped, in `.venv-image`, which holds only the runtime dependencies, so a
test that reads a file the image leaves out, or loads a development tool, fails
here. Without
the set it runs the host alone and says so; `REQUIRE_COMPOSED=1` fails
instead, which is how `mise run preflight` runs it.

Compose builds each extension from its branch named like the host branch, or
from its admitted main when it has none, so a host branch pushed ahead of its
extension work is verified without it. `mise-tasks/git/pre-push` refuses that
push: with a set named, every extension checkout with work off main must carry
the host branch's name and be pushed first. The global pre-push hook runs it
with git's refs on stdin.

### Heavy gates take turns

Two suites at once exhaust a small machine's memory, so the heavy tasks (`fast`,
`check`, `ci`, `checks`, `tests`, `preflight`, the suites, the browser gates,
`demo:screenshot`, a run of tests that is not narrow, below, and `dev:look`
except a look at named pages with no `--base`, `--perf` or `--deep`) run under
`tools/lib/heavy.sh`,
which holds an exclusive lock on `~/.cache/severino/heavy.lock` (under
`XDG_CACHE_HOME` when it is set) for as long as the task runs. Every checkout
and worktree shares that file. A heavy task started while another holds it
prints a line naming the holder (its mise task and checkout, how long it has
held the turn, and its pid) and waits for the turn, naming
the holder again every `SEVERINO_HEAVY_EVERY` seconds (a minute by default),
so a holder that has stalled shows as that rather than as this task hanging.
The wait is bounded by
`SEVERINO_HEAVY_WAIT` seconds, two hours by default, the longest a heavy task
may run; past that it exits 75 (`EX_TEMPFAIL`) naming the holder again. A
waiting task stops nothing: what ends a hung holder is its own timeout, below.
A heavy task run by one that holds the lock (`ci`
running `browser`, `check` running `suite:as-image`) runs inside its turn. The
lock goes with the process, so a killed gate frees it. CI runs one job per
machine and takes no lock.

A TOML task takes the turn by extending the `heavy` template in `mise.toml`,
`django-heavy` when it is a Django gate, or `django-browser` when it is a
browser gate, which names its test classes in `vars.classes`; a file task sources
`tools/lib/heavy-turn.sh` and sets `#MISE timeout`.

Whether a run of tests is heavy is `tools/gate/turn.py`'s to say, from
manage.py test's arguments: a run is narrow when labels name what it runs, each
a module or something in one, and none of the tests they load drives a
browser. No label, a package label or a browser makes it heavy; a test
database does not. A task whose tests are fixed takes the turn exactly when
they are heavy (`tests:api-properties` and `demo:build` never do), and
`tools/gate/tests/test_turn.py` holds every task to that. A task that runs the
tests it is given (`checks:source`, `suite:as-image`) sets `test_arguments`
before it sources `heavy-turn.sh`, so `mise run checks:source <module>` starts
at once while every source test waits; `mise run test` asks the same question
in its run (`tools/gate/at.py`), and `--at` always takes the turn. `dev:look`
alone decides by its pages, since its run is always one browser test and only
the pages say how much it draws. Each heavy
task has a timeout (30 minutes for a gate, two hours for an aggregate), so a
hung gate stops and frees the turn. `heavy.sh` runs the command as a process
group of its own and stops that group when it is stopped, since a `mise run`
inside another task signals only the pid it started; the command never holds
the lock itself, so the turn ends with `heavy.sh`. `tests/shell/test-heavy.sh`
holds both: every heavy task has a timeout, and one that hangs past it, run by
hand or from inside another task, frees the turn. A test that stalls frees it
well before that: the stall bound below fails it within minutes.

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
- **System tools:** `ssh-keygen`, `sqlite3` and `zstd` for the shell suites.
- **A container runtime** for the `image` gates. CodeQL wants about 2 GB of
  memory.
- **Network, the first time:** mise downloads and verifies each tool.
  Scorecard's vulnerability check queries `api.osv.dev` on every run.

### Browser layout

`mise run browser` installs the WebKit the locked Playwright pins (HQ is read
in Safari) and runs the package `hq.platform.core.tests.browser`. It renders the
dashboard, service, connections, machine, topology and project pages over a
synthetic `example.*` estate at phone through desktop widths, with JavaScript
off and on, and checks that nothing escapes the page sideways, a table fits
its region at every width, nothing moves once the server's page is drawn,
grid rows end together, siblings never overlap, and the rules of
`hq/platform/core/browser_audit.js` hold. A failed subtest saves a screenshot
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
`mise run test <label>` once `mise run setup:browser webkit` has installed the engine.

A failure only CI's Linux shows is reproduced with `mise run dev:linux-browser`
and a gate (`browser:layout-chromium-1`) or test labels, drawn in Chromium
unless `--engine` names another. It runs them on this checkout inside the
Playwright image `ci.yml` gives the Browser job, as linux/amd64 with the job's
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

A loaded CI runner draws a page's frames late, so a check that reads a page
before a script's frame or idle callback has run can fail there and pass on an
idle machine. `HQ_BROWSER_SLOW=<ms>` on any browser gate or `mise run test`
label, or `--slow <ms>` on `dev:look`, draws every page that way: each
animation frame comes that many milliseconds late, and an idle callback waits
three times its timeout. `HQ_BROWSER_SLOW=150 mise run browser:navigation`
reproduces such a flake without loading the machine.

What the browser gates read from the environment:

| Variable | Set by | Does |
| :--- | :--- | :--- |
| `HQ_BROWSER_ENGINE` | development | the browser gates' engine: `webkit` by default, `chromium` or `firefox` |
| `HQ_BROWSER_CHANNEL` | development | a Playwright browser channel for that engine |
| `HQ_BROWSER_FACE` | development | `fallback` draws the browser gates with Inter withheld, in the stack's next face (`--fallback-face`) |
| `HQ_BROWSER_SLOW` | development | milliseconds each animation frame comes late in the browser gates, idle callbacks waiting three times their timeout (`dev:look --slow`) |

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
until its timeout.

The example estate's values have the shapes and lengths real providers give
(both tailnet addresses and a MagicDNS name per device, long image tags, a
zone with many records), so a layout that holds there holds on real data.
Each is invented or shared by every network:
`hq/platform/core/tests/test_example_estate_values.py` fails on an address or
host name outside the reserved and shared ranges it names.

`mise run dev:look <pages>...` is the same browser for looking rather than
gating: it saves a full-page screenshot of each page at each width under
`var/look/`, which each run empties first so every shot there is its own.
A page is named as `estate/<path>`, a path (`/infrastructure/containers/`) or
a route name (`control_plane:containers`), one per argument; an unknown one is
refused before anything runs, with the nearest routes named, and a page that
answers anything but 200 fails the run.
The look keeps its test database in `var/dev-look/` through Django's
`--keepdb`, so a second run creates nothing; a run that did not finish makes
the next one start fresh, a look with `--base` keeps none, and an edited
migration needs `rm -r var/dev-look`.
The pages and the rule asked are checked before anything is drawn, and a rule
that does not exist is refused with the list of those that do. A stress page
builds only its own estate, and the dense one, which answers what a page asks
for on opening. `estate` names the example estate's pages, drawn on the data
set `--seed` names (`showcase` by default, any name `hq.platform.core.seeds`
registers); `--deep` adds every page they link to (each record, each command form), answered by HQ's views as
the browser asks, every request marked as a prefetch so a page that would act
or reach out is refused and reported instead. `--rule survey` runs every
check the gates hold (`BrowserGate.findings`) at any page and width, so what it
finds on a page the gates read is what they fail on. Markup a vendored script
draws (`SELECTORS["vendored"]`, the API reference's viewer) is held by its own
tests, not judged. `mise run dev:look --deep --rule survey estate/all` walks
the whole estate. What a rule finds is written beside the screenshot
(`<page>-<width>.txt`) as each page is looked at, so a run stopped part way
keeps what it found, and page by page to `var/gate/look.txt` once the run
ends; the run prints each page and width as it starts and, once it has
looked, each shot it took, how many findings it kept, and every console
error, uncaught script error and policy refusal the page had, then the
checks it ran and a verdict naming every page that failed. Those errors are
judged as a rule's findings are: without a base any fails the run, against
one only a new one does. With `--perf`, on by default with `--base`,
`perf:report` then measures the estate pages asked, beside the base's, in the
same heavy turn, and its table follows the look's verdict; a page it cannot
serve is named once. `--base <ref>` looks at the same
pages at `<ref>` first, in a temporary worktree it removes however the run
ends, with this checkout's test files laid over it as `test --at` does, and judges by this checkout's rules, its output relayed line by line
after the ref as it runs, writes what is new on this checkout and what
it fixed to `var/gate/look.txt` under the commits both ran at, and prints a
line for each new finding and the verdict. Every probe reports all it finds,
and findings are matched as a multiset by page, width, check and a key with
every UUID, hex id and origin panel's id taken out, each size and contrast
kept to its power of four, and every other number and the record named after
a measure taken out, so one whose row number moved, whose overflow grew by a
few pixels, or that names another record is neither new nor fixed, and one grown many times over is new. With `--columns` or `--eval`, each line the
base measured otherwise is printed under this checkout's, marked with the ref.
`--css` takes CSS text or a file of it, a relative path read from the
checkout. `--eval` runs before the screenshot, so what a script changes is
seen. `--dialog <id>` (`connection`, or `all` for every dialog a page
declares) also shoots each page with that dialog open
(`<page>-<width>.dialog-<id>.png`): it presses a control that opens it, after
any menu holding that control, so the page's own wiring opens it, and waits
for its parts to load; `--eval` and the rule run again there.

`mise run perf:report <pages>... [--image] [--seed <name>] [--base <ref>]
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
(its `--base` run shares that instant), so a time a script reads is drawn
alike in both, and each screenshot is taken with animations finished. A page
taller than the window is also cut into window-sized bands beside its full
screenshot (`<page>-<width>.band-01.png` on), one screen to read at a time.
A page that could not be looked at (refused, no `main`, a rule that could not
run) is shot as it stands, as `<page>-<width>.failed.png`, and its path
printed. `--open` opens every disclosure, round after round until nothing
more opens, so a disclosure a region brings in when another opens is opened
too; then a rule is run again with one popover of each kind shown on its own,
as a reader shows one, shot that way (`<page>-<width>.popover-<id>.png`),
and what it finds past the page beneath is said under that popover's id and
its shot. A kind is the popover's classes and the table and column it
sits in, so a list's many panels from one partial cost one run. `--engine chromium` or `firefox` draws in
another engine, installed first (`webkit` by default).

`--columns` prints each table's column widths as drawn and at min-content and
max-content. With `--expand` an open row holds the columns at the widths they
had before it opened (`tables.js`): a held table's line says its drawn widths
are those, and its min and max are measured on a copy without the hold.

`--eval <file.js>` reads a page the way a throwaway probe would, in one
command: the file holds a function, called with nothing, or one expression,
run on each page at each width once `--css`, `--open` and `--expand` have
applied; what it returns prints under the page and width (as JSON unless it is
text) and is kept beside the screenshot as `.eval.txt`. A computed style, or an
element's chain of ancestors with their widths:

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
mise-tasks/               every task outside mise.toml: one TOML file per CI job (checks.toml, ...) and operator.toml, and beside each a folder of script tasks
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
