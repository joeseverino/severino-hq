# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.
How agents work on HQ through these gates: [AI](AI.md).

## Setup

Install [mise](https://mise.jdx.dev) and a `python3`; `mise install` brings uv
with the other tools `fast` and `check` run.

A tool only a heavier gate needs (CodeQL, age) is `lazy` in `mise.toml`: its
first use installs it, checked against `mise.lock` like the rest.

Go is the one tool `mise.toml` does not pin itself. It reads the release from
`controller/go.mod`'s `toolchain` line, which Renovate moves, so mise loads
`mise.toml` only beside that file.

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

Every gate and server is a mise task, and
[AGENTS.md](../AGENTS.md#start-here) says which to start from. mise prints
the rest live, so no doc copies it:

| To see | Run |
| --- | --- |
| the tasks to start from, each with when to reach for it | `mise tasks` |
| every step they run as well | `mise tasks --hidden` |
| what one task takes: its flags, arguments and environment | `mise tasks info <task>` |
| a task's examples, where it carries some | `mise run <task> --help` |
| whether this checkout can compose its extensions, with the fix for each gap | `mise doctor project` |

### Proving a test fails without the fix

`mise run test --at <ref> <label>...` runs this tree's test files,
uncommitted work included, on `<ref>`'s code. Reach for it to show that a new
test fails on the old code, or that a failure was already there. The ref is
the code before the fix, so a fix's test is proven with `--at <fix>^`.
`mise tasks info test` says what `--at`, `--keep` and `--go` take, and how a
shell suite's path is run.

The closing line names each support file laid over that replaced the ref's
own copy. A run that replaced the code its test holds has tested this tree's
code, so a Python or Go pass with such a file runs once more with each one
kept at the ref. That second run's outcome is the verdict.

When that second run does not import or build, the kept files hold a fixture
the new test needs, which proves nothing: `--keep` only what holds the fix. A
shell suite's support is the fakes it reads, so its pass runs once.

A test that drives `at.main` patches its `ROOT`, as `TestAtRefTests` does,
since `changed_tests` lists files with git.

### Audits

The `audit:` tasks measure without writing anything tracked, so a read-only
auditor may run any of them. mise runs each in its OS sandbox (the `audit`
template), where a write outside the temporary and Go build cache paths
fails; `audit:memory` also writes `var/perf`. `audit:image` and `audit:mcp`
write as they measure, so they run outside it. A `.venv` out of sync with
`uv.lock` fails an audit, since uv cannot sync it there: run `uv sync`.

| Task | What it answers | Example |
| --- | --- | --- |
| `audit:ruff <rules> [paths]...` | Ruff's findings, counted per rule | `mise run audit:ruff SLF001 '!**/tests/**'` |
| `audit:ast [globs]...` | per file, the docstrings over 8 lines and the `getattr` calls of a literal name with a default (`tools/audit/ast-grep.yml`) | `mise run audit:ast 'hq/**'` |
| `audit:dead` | the names `hq/` and `hq_sdk/` define that nothing outside tests reads (a test-support module's, `testing.py` or `*_testing.py`, that nothing reads at all), as vulture finds them, with what it cannot see handed to it as uses (`[tool.vulture]`, `tools/lint/vulture-whitelist.py`) | `mise run audit:dead --fields` |
| `audit:layers [importer] [imported]` | the imports between core, application and domains, counted per pair both ways, or one pair's listed by module (grimp) | `mise run audit:layers core domains` |
| `audit:hand-rolled` | the page-class check's count: the classes host templates hand-roll instead of drawing a shared part, as totals, per template, and any difference from `HAND_ROLLED`; the number a UI report gives before and after (`test_host_templates_draw_only_the_shared_parts_classes` reads the same) | `mise run audit:hand-rolled` |
| `audit:imports <path>` | the files that import one, string imports included | `mise run audit:imports hq/platform/core/static.py` |
| `audit:churn <since>` | each tracked source file's commits since a date, most first, generated and vendored files left out (`.gitattributes`); reach for it before a refactor or split | `mise run audit:churn 2026-10-01` |
| `audit:go [linters]` | golangci-lint's findings, counted per linter | `mise run audit:go nilerr,gocritic` |
| `audit:go-dead` | the controller's functions no main reaches on Linux, as deadcode finds them (a test-support package's that nothing reaches at all) | `mise run audit:go-dead` |
| `audit:memory` | each module's own share of Python memory at boot and after a pass over the navigation, extensions composed; `--after` adds the paths boot never takes, each with its import times (`python -X importtime`); it ends by naming the `var/perf` JSON that holds every row its tables leave out | `mise run audit:memory --host-alone --after oidc,mcp-call,outbound` |
| `audit:advisories` | `lint:audit`'s own report, run as a read: uv audit's advisories and adverse statuses per package, and osv-scanner's table of each advisory over every tracked lockfile (`tools/pipeline/audit.sh`); needs the network | `mise run audit:advisories` |
| `audit:dependency-weight` | each locked Python distribution's installed size (its RECORD, through `importlib.metadata`), runtime apart from the dev groups, and each package's and module's bytes in every shipped Go binary (go-size-analyzer) | `mise run audit:dependency-weight` |
| `audit:image [image]` | a built image's layers (size, instruction, the parts of HQ each holds), what a container sees by part, site-packages' largest, and dive's wasted bytes | `mise run audit:image severino-hq:ci-local` |
| `audit:ci-timings <run>...` | where CI runs' time went, each job's end lag included | `mise run audit:ci-timings 41175916328` |
| `audit:docs-readability` | each doc's Flesch-Kincaid grade, prose words, and longest paragraph and sentence, longest paragraph first (`tools/lint/vale/audit.ini`) | `mise run audit:docs-readability` |
| `audit:bcd <feature>...` | each browser's first supporting version, from browser-compat-data, Safari first | `mise run audit:bcd css.properties.container-type` |

A path or glob narrows a count, and `!glob` leaves files out, so
`mise run audit:ruff SLF001 '!**/tests/**'` and
`mise run audit:ruff SLF001 '**/tests/**'` count source and tests apart.

## Development servers

Each server is a mise task, and its env says where it listens and which
database it opens; `tools/dev/dev.sh` refuses what env cannot express.

| Server | For |
| --- | --- |
| `dev:scratch` | a throwaway server on this checkout's scratch database, loopback only |
| `dev:scratch --no-reload` | one that only needs to answer: one process instead of three |
| `dev` | the long-lived server behind a TLS proxy, configured by the gitignored `dev.local.toml` (copy `tools/dev/dev.local.example.toml`) |

Every one collects versioned assets and runs Uvicorn with reload on the
production ASGI path. It watches this checkout and each extension's sources
through watchfiles (the `dev` group): a Python file or a template restarts
it. Nothing under `.claude/worktrees`, `.venv` or `var/` does, so another
worktree's edits leave it running. The extension set is the one
`mise.local.toml` names ([The extension set](#the-extension-set)).

A server migrates its database before it starts, and each worker a reload
starts applies the migrations it ships that the database has not recorded
before it serves (`hq/config/dev_asgi.py`, the application factory Uvicorn
loads), as the container migrates at boot. A landing that adds a migration
needs no restart.

Only `dev` reads `dev.local.toml`, and a new worktree does not copy it, so the
others never take its bind or database. Each checkout has its own scratch
database, so servers in different worktrees run side by side; a second server
on one database is refused. `dev` refuses a linked worktree and a bind past
loopback and the tailnet range; scratch refuses anything but loopback.

Every loopback server shares the browser's cookies for `127.0.0.1` whatever the
port, so signing in to a second one signs the first out: open the second as
`http://localhost:<port>`.

Python dependencies come from `uv.lock`. `mise.toml` sets `UV_LOCKED`, so
every task's `uv run` refuses a lock that `pyproject.toml` has moved past, and
no virtualenv is made by hand. `uv lock` and `uv add` honor it too: change the
lock outside a mise task, or with `UV_LOCKED=0`.

The default groups are `dev` and `tools`; the browser gate adds `browser`. The
image installs a hashed export with every default group omitted.

## The gates

Every gate is a task named `<job>:<gate>` (`mise run lint:ruff` runs one).
Each CI job is named for the task it runs (`lint`, `test`, `browser`, `build`
and `scan`) and reaches its job's gates through a wildcard.
`GateReferenceTests` holds every job id to its task.

A gate is declared in its job's file in `mise-tasks/` (`mise-tasks/lint.toml`
holds `lint:*`), or beside it in `mise-tasks/lint/` when its body is a script.
`mise.toml` keeps the tools, the environment, the templates, the development
servers and the `lint` and `scan` tasks.

A task outside `mise.toml` names no file in its env. mise reads an env path,
and renders `config_root` inside env, from the directory of the file that
declares the task (`GateReferenceTests`).

On a pull request CI's **Ready** job needs every other job and fails when one
did not pass. It is the one check the `main` ruleset requires, so a gate added
to a job never touches the repository's settings (`VerdictNeedsTests`).

### What a pull request runs

A pull request runs Lint, Test, Test's Go tests and Browser only where its
change can affect them, and Ready counts a leg left out as passed. Each leg
declares the files it reads once, as the `sources` of the task its job runs:
`lint` in `mise.toml`, `test` in `mise-tasks/test/_default`, `test:controller`
in `mise-tasks/test.toml` and `browser` in `mise-tasks/browser`. CI's **Legs**
job runs `tools/pipeline/legs.py`, which asks mise's `--affected` which of
those tasks the change touches. A changed path that no leg's sources name runs
every leg, and `test_legs` fails on any tracked file in that state, so a new
top-level path is placed in the sources of each leg that reads it. Scan,
Build, Build verify and Composition run on every pull request.

To see what a change would run, against the base it merges into:

```sh
python3 -m tools.pipeline.legs origin/main HEAD
```

A push to `main` runs every leg. One whose pull request proved its tree
promotes that image, and skips Lint, Test and Browser only when the pull
request left none of them out ([Checked once](Deployment.md#checked-once)).

Selection by GitHub's `on.pull_request.paths` gates a whole workflow, never a
job, and a required check whose workflow never starts holds the merge, so it
cannot serve Ready. A path-filter action would restate the paths the tasks
already declare.

`mise run check` answers "will the pipeline accept it?" and keeps going past a
failure to name every gate that failed.

`fast`, `check`, `lint`, `test`, `browser`, the composed suite and an
extension's check (`tools/pipeline/check-plugin.sh`) end on a verdict line
(`tools/gate/verdict.py`). It names each failed leaf task and test and the one
command that reruns just those. A gate whose steps queued for the
[heavy turn](#heavy-gates-take-turns) says how long beside its total
(`passed in 664.5s, 410s of it waiting for the turn`), so the rest reads as
working time.

The whole verdict is kept in `var/gate/` beside the run before it, with each
failed test's exception and traceback and each failed task's last lines
(`HQ_GATE_VERDICTS` names another folder, as a gate tool's test does for
every process it starts). A
gate's first line names that file, since the head of a long output is the
part a reader shown only an excerpt of it always sees.

The file opens on `tree <hash>`, the tree a commit of the checkout's files
would hold (staged or not, tracked or not, as both the repository's and the
user's ignore rules leave them), taken as the gate starts and again as it
ends; the verdict line follows it. A pass is then attested by the file alone:
it holds for a commit whose `git rev-parse <commit>^{tree}` that line names,
which is how an agent's hand-back states its gates. A checkout that changed
during the run judged no one tree, and the line is `tree -`. `HQ_GATE_TREE`
names another checkout, as an extension's check names its package's.

A `dev:look --rule survey` without `--css` is kept so too, per engine, in
`var/gate/look-survey-<engine>.txt`, the pages and widths it judged beneath;
one that found anything or judged nothing fails. Landing a change to
`templates/`, `static/css` or `static/js` reads both engines' records for the
tip's tree.

The suite runs one worker per core (per performance core on Apple silicon);
`DJANGO_TEST_PROCESSES` overrides it. `mise run fast --parallel 1` rules
parallelism out when a failure looks order- or isolation-dependent.

A worker imports lazily, as the process that starts the run does. The suite
stores passwords with Django's fast hasher ahead of HQ's Argon2
(`hq/config/settings.py`), and `test_password_hashing` holds production's
list.

### What `fast` runs

On an 8 GB Apple silicon machine:

| Run | Takes |
| --- | --- |
| `mise run test <label>` | a few seconds |
| `mise run lint:prose` | about five seconds |
| `mise run lint` | about a minute |
| `mise run fast` | one to two minutes |

`mise run fast [BASE]` maps the changed files against the merge base (`BASE`,
`$CHECK_BASE`, then the branch HEAD lands on, then `origin/main`, `main`).
The branch HEAD lands on is its own branch's upstream, or else the local
branch tracking a remote one whose history HEAD left most recently: a
worktree's branch tracks nothing and is made from the branch it lands on.
`--list` prints the selection.
The tests it selects are:

- the test modules that import a changed file, directly or through one module
  between;
- every test of the changed Django apps, and every test that names one (its
  URL namespace or app label);
- every test that reads the tasks, when a task definition or a file a task's
  run names changed;
- the architecture tests;
- every test tagged `tree`, when a module under `hq/`, `hq_sdk/`, `tools/` or
  `tests/` changed, the packages the suite lives in. Such a test finds its files by walking this checkout, which no import shows
  (`test_tree_tag.py` holds the tag to the walks; a test that patches the
  `ROOT` of the code it drives walks a fixture and carries none);
- the extensions' suites, composed, when `mise.local.toml` names a set and the
  change reaches what extensions render through (`templates/`, `static/`,
  `hq_sdk/`, the shared table, page, UI and moment modules, template tags).

Beside the tests it runs each quick gate the change affects, as mise's
`--affected` reads it from the merge base: a quick gate is a task tagged
`fast`, and it is affected when a changed or deleted file matches its
`sources` (`!` patterns take paths out) or its own definition changed. Every
quick gate's sources also name the tool pins and task definitions, and each
one that runs Python its dependencies (`pyproject.toml`, `uv.lock`), so a
change to one runs every gate that reads it. The controller's Go tests
(`test:controller`) are a quick gate too: they take the heavy lock
themselves, so they run once the rest has passed, and Go's own test cache
reruns only the packages a change reached. Each quick gate declares
`outputs = []`, so a direct `mise run` of it never skips as up to date. The
shell gate also runs when a path the change touched is one
`tools/gate/shell-sources.sh` names (a deleted one as the base had it), which
no glob can say. `mise run --tag fast --affected --affected-base <ref>
--affected-json` names each gate it would pick and why, running none. A lint gate that reaches the network, or whose check
fast makes another way, is left untagged and named in `EXEMPT` in
`tools/gate/tests/test_fast_selection.py`, with why; that module fails on a
`lint:*` task in neither, and holds each gate's `!` patterns to the
exclusions its tool declares.

A test module that a `test:*` task runs by its label in a dependency group's
environment (`uv run --group fuzz`), which the tests' environment lacks, is
never a label. When the selection above would reach it, its task runs as a
quick gate instead, read from the task's own `run`.

The quick gates and the tests run in one `mise run`, in the config
environment `mise.fast.toml` sets: mise's summary output, where a gate that
passed prints its `passed in` line as it ends and one that failed prints
whole as it ends. The tests run as `fast:tests` (`mise-tasks/fast/`), whose
own output setting outranks the environment's, so they print as they run,
their wait for the heavy turn included; `--output` or `MISE_TASK_OUTPUT`
would outrank both.

mise stops the run at the first failure, and the verdict's last line names
the rerun. mise stops no task still waiting for a lock, so a quick gate that
takes the heavy lock itself (the shell gate, for its suites) runs after, in a
run of its own, once the first has passed. The selected tests that read every
browser gate module (each imports `browser_modules`: the selector guard among
them) start no browser, and run first as `fast:source`; the tests start only
once they pass, so a refusal there ends the run in seconds.

A test run prints its failures, each with what the test printed, and nothing
of a passing test or of its own progress: the runner sets Django's `--buffer`
and verbosity 0 by default. `mise run test <label> --no-buffer -v 1` prints
it all.
A debugger prompts as usual in a serial run (`--parallel 1`), from
`breakpoint()` or `--pdb`: the runner points `breakpoint()` at the process's
own output, past the buffer.

Each Django gate opens a database of its own, and its tests run with `DEBUG`
on, the one pass that does.

A pass on a checkout that held still through the run records the tree its
verdict file opens on, as an empty file named by that hash under
`$(git rev-parse --git-common-dir)/severino/fast-passes/`, which every
worktree shares and which a re-signed commit keeps. It records one only when
the run proves the tree: its base's tree has a record, so what the change left
unselected had passed, or the run selected every test and quick gate. A base
far enough back selects them all (`mise run fast $(git rev-list
--max-parents=0 HEAD)`), the way to record a first tree. The agent hooks hold
a brief's base to a recorded pass.

### What `check` runs

`mise run check` runs what CI's gate jobs run, one task at a time so no two
suites share the machine's memory:

| Job | What it runs |
| --- | --- |
| `lint` | every `lint:*` gate, the Go controller's static half and the derived files among them |
| `test` | the `test:*` gates on every Python in `PYTHON_VERSIONS`, the suite as production configures it with `DJANGO_DEBUG` unset and the coverage floor, then the Go controller's race tests once |
| `browser` | the browser gates ([Browser layout](#browser-layout)) |
| `build` | the image build and what an image must pass |
| `scan` | CodeQL's analysis, which finds no alert here; in CI, no alert GitHub holds open on the analyzed ref, so a dismissed one passes |

A Go fuzz target's seeds and kept inputs run as plain tests in `test`; the
search for new inputs is `mise run fuzz`, outside every gate, which runs each
target for a bounded time, as the weekly Fuzz workflow does. A parser of
input from outside a privilege boundary has a `Fuzz` target in its package's
tests that holds its promise: a refusal is the parser's own error, and what it
reads round-trips or keeps the invariant it documents. An input a run finds
failing is kept in that package's `testdata/fuzz` with its fix.

A setting whose default follows `DEBUG` is held by a test that loads the
settings both ways. The suite inside an image runs once, in Compose, on the
composed image production runs.

`mise run check --release` then runs the composed suite with the extensions
required and checks the deploy host, read-only, on the committed `HEAD`. Its
verdict is kept apart, in `var/gate/check-release.txt`.
`mise tasks info check` says what `--remote` and `--skip-host` do
([Deployment](Deployment.md)).

A test tagged `source` runs once, on the checkout, in `lint:source`
(`mise run lint:source <label>...` runs only the tagged tests those labels
name). A whole-suite run leaves the tag out; a run that asks for a tag no test
carries fails.

A source test that reads the whole tree leaves the parsing out of the test
worker, which would hold every tree it parsed for the rest of the run, and the
`tree-parsed-apart` rule refuses an `ast.parse` in a loop over a tree walk
outside the modules run through `apart`:

- A check that is one match is a rule in `tools/lint/ast-grep/rules`, with its
  cases in `rule-tests/`. A test that holds paths the rule's `files:` leave
  out, an installed extension's, scans the same rule over them
  (`PageTrailTests`).
- A check that judges facts across files reads them through `scan(rules,
  *paths)` in `hq/platform/application/tests/facts.py`, one ast-grep run that
  streams back only the nodes its rules match. It serves only a test tagged
  `source`, since the image ships no ast-grep. Those fact rules sit inline
  beside the Python that judges them, and a planted case runs through the same
  scan, since `ast-grep test` skips a rule of `severity: off` and still
  reports ok.
- A walk that stays Python runs through `apart(walk, *args)`
  (`hq/platform/application/tests/apart.py`), in a process that exits; the
  walk takes and answers JSON. `interface_text.read(run=apart)` is one, since
  the image runs it and ships no ast-grep.

Each is a walk to `tree.is_walk`, an ast-grep command line included, so the
test carries the `tree` tag (`test_tree_tag.py`).

The wording rules are Vale's, in `tools/lint/vale`: `.vale.ini` there says
which files each style reads, `styles` holds the rules, and `lint:prose` runs
them. `hq/platform/core/plain_words.py` hands Vale the template attributes and
tag literals it cannot read. `uv run python -m hq.platform.core.plain_words
--list <paths>` prints every finding in those paths at its line, Vale's on the
text and its own on the rest, before the baseline in `prose_baseline.py` holds
any; `vale --output=line` alone sees only the text.

A finding names its rule and what to say instead. A word that must stand in
one file is turned off for that file in that `.vale.ini`, with the reason
beside it.

### Derived files

`mise run fix` rewrites every file generated from a source, in the order each
reads the last, then formats as the lint gates read:

1. `hq_sdk/contract.json`, with the SDK's text examples and the table in
   `docs/Plugins.md`;
2. the machine API's OpenAPI document;
3. the controller's contracts from the connection registry, and the Go types
   generated from them;
4. `docs/Connections.md`, from what the providers declare;
5. the served font, cut to the characters HQ prints.

It then lowers each count in `tools/lint/stylelint-suppressions.json` and
`tools/lint/eslint-suppressions.json` that has fallen to what the linter finds
(`fix:suppressions`), so a change that removes a suppressed problem leaves its
count right. A count that rose is a new problem: `fix` leaves it, and
`lint:css` or `lint:js` refuses it.

Each `lint:drift-*` gate checks one against its source without writing. Its
failure names the command that rewrites that file alone, in seconds; `fast`
runs the ones a change reaches. A rebase conflict in one is settled by
`mise run fix`, never by hand.

The OpenAPI document's examples are responses the API's tests received,
recorded in `hq/platform/api/openapi-examples.json`. A recording is a serial
run of those tests under a clock that stands still, with fixed ids, so it
reads the same every run.

`lint:drift-api-examples` records one beside the file and fails when they
differ, and `mise run fix:api-examples` records them again, then rewrites the
document. It is not a step of `fix`, being a run of the suite.

### The extension set

`mise run setup:extensions <checkout>...`, run once per machine in the main
checkout, names the set in the gitignored `mise.local.toml`. It reads each
package of each checkout as admission reads it: `HQ_LOCAL_PYTHONPATH` lists
their `src` directories and `HQ_LOCAL_PLUGINS` the manifests they declare.
mise writes the file and keeps every other value in it, and
`mise doctor project` then checks that the set imports.

A worktree Claude Code makes gets a copy of the file through the gitignored
`.worktreeinclude`, so it composes the extensions' main checkouts. A value of
either already in the environment wins, so an extension worktree is composed
by naming its sources on the command line, one `src` per package, joined by
`:`, and another set by naming its manifests:

```sh
HQ_LOCAL_PYTHONPATH=<worktree>/packages/<name>/src mise run test --composed
HQ_LOCAL_PLUGINS=tests.fixtures.example_hq_plugin.plugin:plugin mise run dev:scratch
```

Only the composed suite, the dev servers and `mise run dev:look --composed`
load the set. The suite and the look load it through
`tools/lib/extension-set.sh`, whose first line names the `src` each package is
imported from, so a run says which tree it composed.

`mise run test --composed` runs the suite as the image does: in a copy of only
the files `Dockerfile.dockerignore` admits, less what the Dockerfile's
`COPY --exclude` keeps out of `/app`, with `DEBUG` off, `SEVERINO_HQ_SOURCE`
stamped and only the runtime dependencies (`.venv-image`). A test that reads a
file the image leaves out, or loads a development tool, fails here.

Without a set it runs the host alone and says so; with `REQUIRE_COMPOSED=1`,
as `mise run check --release` runs it, it fails instead.

That copy is read from the two files by `tools/lib/image-tree.sh`.
`image:verify:tree`, one of the checks `mise run build` runs, holds it to the
built image's `/app`, so a form the reading misses fails the build naming each
path.

The same file holds this machine's other values, each written with
`mise set --file mise.local.toml NAME=value`:

| Value | What it is |
| --- | --- |
| `SEVERINO_HQ_DEPLOY_HOST` | the SSH destination `mise run check --release` checks read-only before a release |
| `SEVERINO_FAILURE_LOG_IDENTITY` | the age identity that opens a failed run's sealed logs (`mise run failure-logs`): a password-manager reference read at use, or a file path |

Compose builds each extension from its branch named like the host branch, or
from its admitted main when it has none, so a host branch pushed ahead of its
extension work is verified without it.

`mise-tasks/git/pre-push` refuses that push: with a set named, every extension
checkout with work off main must carry the host branch's name and be pushed
first. The global pre-push hook runs it with git's refs on stdin.

### Heavy gates take turns

Two suites at once exhaust a small machine's memory. So every step that loads
the machine (a suite, a browser, an image build, CodeQL) waits for one turn
shared by every checkout and worktree, and a waiting task names the holder.

A step of one process (a Django command, a linter, a serial test run) runs
beside the holder. An aggregate (`check`, `lint`, `test`, `browser`) holds no
turn of its own, so its light gates never wait; `build` holds one across its
steps, which share the VM it boots. A one-off command in the built image
(`mise run image:run -- python -V`) holds the turn and the VM while it runs,
so another checkout's stop leaves the VM up under it.

The turn is mise's task lock `heavy`, a name every checkout and worktree on
the machine shares. A TOML task takes it by extending the `heavy` template in
`mise.toml` (`django-browser`), a file task with `#MISE lock=["gate", "heavy"]` in its
header and the line `. ./tools/lib/heavy-turn.sh` before its work; either
declares a `timeout`, which mise starts once the task holds the lock, so
heavy tasks asked together (`mise run browser:layout-1 :::
browser:layout-2`) queue on the turn and each runs in full, and a hung holder
is stopped. A heavy task started inside a turn runs at once, since mise hands
the lock down (`MISE_TASK_LOCKS`), and a holder killed outright frees it.
`HeavyTaskTests` (`tools/gate/tests/test_turn.py`) holds every task to its
side of the line, and `tests/shell/test-heavy.sh` the turn's promises.

A landing goes first. Every task that takes the turn passes the lock `gate`
first, which mise takes in sorted order, so a task waiting there holds
nothing. `mise run landing -- <command>` holds the gate for the command's
lifetime: no other checkout starts a heavy run until the landing ends, and
the landing waits behind only the run already in the turn, since it takes the
gate with a blocking lock that the kernel hands over as the holder ends, where
mise polls. The command's own heavy tasks inherit the gate
(`MISE_TASK_LOCKS`) and take turns on `heavy` among themselves.
`test_landing.py` (`tools/gate/tests/`) runs that order against mise.

A run of tests is narrow, and goes ahead outside the turn, when it is serial
(`--parallel 1`) or each label names a module or something in one, and no
test it loads drives a browser (`tools/gate/turn.py`). So
`mise run test <module>` starts at once. A task whose weight depends on what
it is given (`test`, `fast`'s tests, `lint:source`, the composed suite,
`dev:look`, `browser --linux`) runs its heavy runs through the hidden task
`turn`, which holds the lock (`tools/lib/heavy-turn.sh` for a file task)
under the gate's own bound, `HQ_TURN_BOUND`: `mise run --timeout` would count
the wait too.

A task that waits for the turn without holding the lock (each of those, and
an aggregate whose script runs `mise run` on one that waits) declares
`SEVERINO_WAITS_FOR_HEAVY_TURN="1"` in its env, the one place
`mise tasks ls --json` shows it without running anything; mise lists no edge
for a `mise run` inside a script. The agent hooks read it beside the lock, and
`TurnMarkTests` (`tools/gate/tests/test_turn_mark.py`) derives the set from
every task's scripts and holds the marks to it.

A waiting task names the holder, its checkout, how long it has held the turn
and its pid, again every 30 seconds:

```text
[lint:shell] waiting for lock heavy, held by test:suite in ~/Code/Projects/severino-hq (pid 4121, for 3m02s)
```

`mise run --lock-timeout 5m` gives up after that long, naming the holder.

Every heavy run ends with one line on what its command's process tree spent
(`tools/lib/measured`), so each run is its own before-and-after measurement:

```text
heavy: 2m41s wall, 14m05s CPU, load average 6.10 5.80 4.20, peak memory 3.21 GB: gate 60 MB, test run 450 MB, workers 2.40 GB (one at most 380 MB), children 300 MB (one at most 210 MB); sampling took 0.4s CPU
```

The CPU is user plus system time over every descendant the command waited
for. Another worktree's load moves the wall time and leaves the CPU alone, so
"no slower" is judged on the CPU. The peak is the most the tree held at once,
sampled every two seconds: phys_footprint (`footprint -p`) on macOS, the
proportional set size with its swapped share on Linux. Beside it is each
role's own peak, read from command lines: the gate (the task's shell, uv,
mise), the test run (the Python whose script is `manage.py test`, never a
wrapper that names it), its workers, and children under them such as a walk
run apart. A worker's and a child's line also gives the most
one process of its role held. The sampling's own CPU is stated last and kept
out of the tree's. The template's `shell`, the line every heavy file task
sources, and the `turn` task each run their command under it, and
`HeavyTaskTests` holds every task that takes the lock to one of the three. A
heavy run inside a measured one adds no line of its own, and a narrow `mise
run test` runs again under it, so measuring one label is that one command.

The line is a report, never a check: load and the mix of tests move both
memory and time, so nothing fails on them, and the command's exit status is
the run's whatever the measuring did. A measurement it cannot take (no
`footprint`, a sample that failed) is named in the line in its place.

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
- **A container runtime** for `build`'s gates, `browser --linux` and
  `perf --image`. CodeQL wants about 2 GB of memory. On a Mac each of those
  tasks starts colima's VM when no daemon answers and stops it when the run
  that started it ends (`mise-tasks/image/docker`), since a running VM never
  hands its memory back to macOS; a VM started by hand is left alone.
- **Network, the first time:** mise downloads and verifies each tool.
  Scorecard's vulnerability check queries `api.osv.dev` on every run.

### Browser layout

`mise run browser` installs the WebKit the locked Playwright pins (HQ is read
in Safari) and runs the package `hq.platform.core.tests.browser`. It renders
the dashboard, service, connections, machine, topology and project pages over
a synthetic `example.*` estate at phone through desktop widths, with
JavaScript off and on.

It checks what a reader would lose:

- nothing escapes the page sideways;
- a table fits its region at every width, and keeps every table check there
  with its rows open;
- no text runs out of its box or over other text;
- nothing is out of reach;
- text and controls keep their contrast;
- the unwanted-scrollbar rule of `hq/platform/core/browser_audit.js` holds;
- [axe-core](https://github.com/dequelabs/axe-core)'s WCAG A and AA rules and
  its best practices hold, text contrast in each palette, run as every probe
  is so a page's policy never meets it
  (`hq/platform/core/tests/browser/probes/axe.py`, whose `LEFT_OUT` names each
  rule it does not hold, and why);
- in the Chromium legs, each page's Cumulative Layout Shift stays within 0.1.

`setup:lint-tools` installs the axe-core `tools/lint/package.json` pins before
every browser gate. Taste (evenness, placement, row height) is written in
[Design](Design.md#held-by-eye) and judged by eye.

A failed subtest saves a screenshot of its page as it failed, named for its
page, width and rule (`example-two-402-overflow.png`). It goes in a
`<test>-*` folder of `var/browser-failures/<task>/`, which the next browser run
of that task replaces; a test that fails outside its subtests saves `page.png`
there.

Beside each screenshot is a Playwright trace of the same name (`.zip`): what
the test did since its last failure, each step with the page as it stood.
`uv run --group browser playwright show-trace <file>` opens it. CI uploads
every such folder of a failed gate.

A check is declared once, beside its probe: an entry of its probe module's
`CHECKS` (`hq/platform/core/tests/browser/probes/__init__.py` says what an
entry holds), which the survey collects from every module there. Every check
the survey runs is proven able to fail. `PLANTS`
(`hq/platform/core/tests/browser/planted.py`) holds the smallest defect each
must find, `PlantedBrowserTests` requires it found, and a new check lands as
its `CHECKS` entry and its plant, never an edit to `survey.py`.

A check selects markup only through `SELECTORS`, and
`hq/platform/core/tests/test_browser_selectors.py` fails on a selector no
template renders.

A browser test that fails only sometimes is a race a reader can meet.
Reproduce it by taking the reader's steps back to back, which opens the race
where CPU load does not, and wait on the state the page reaches when it
settles, never on a longer timer.

#### The two estates

The stress estate (`build_estate()` in
`hq/platform/core/tests/test_browser_fixtures.py`) breaks things on purpose:
long names, many rows, every state at once, on the pages it was built for.

Every list page is also filled from its own model
(`hq/platform/core/tests/test_browser_derived_fixtures.py`). Each view drawn
through `TableListMixin` is found in the URLconf, and its rows carry every
field at its length, money at the largest a household records, and the
longest label of every choice. So a new list or field is measured at its worst
the day it exists (`dev:look derived/<app>/<name>` shows one).

The example estate (`hq/platform/core/example_estate/`, the homelab the demo
and `manage.py seed showcase` show) is read by
`hq.platform.core.tests.browser.estate` on every page the navigation declares.
It is read with script on and deferred parts loaded, at 402, 820, 1360 and
1680 pixels and at `WIDE_TABLES`, in the light palette and the dark. A finding
names its check, page, width and theme.

Its tables are also read at every shape of the layout, in a leg of their own
(`browser:table-fit`, `hq.platform.core.tests.browser.table_fit`): from 320
pixels up, at each edge of the stylesheet's viewport queries and wherever a
box around a table changes shape between two, every row a reader can open is
opened and every table check is read, with what opening the rows moved. A
finding names its check and the first width it was found at.
`TableSweepPlantedTests` plants a defect of each kind in a span of widths no
other gate reads, and requires the sweep to find it there.

`WIDE_TABLES` is read from the stylesheet: the narrowest window where a
table's region, on the page and in a card, has passed the breakpoint its wide
rules need. So the columns that keep their line are measured with the least
room; the stress estate is held there too.

The example estate's values have the shapes and lengths real providers give
(both tailnet addresses and a MagicDNS name per device, long image tags, a
zone with many records), so a layout that holds there holds on real data.
Each is invented or shared by every network:
`hq/platform/core/tests/test_example_estate_values.py` fails on an address or
host name outside the reserved and shared ranges it names.

#### Engines and legs

| To run | Command |
| --- | --- |
| both estates in another engine | `HQ_BROWSER_ENGINE=chromium` or `firefox` |
| the legs named | `mise run browser <leg>...` (`layout-1`, `demo`) |
| one browser test | `mise run test <label>`, once `mise run setup:browser webkit` has installed the engine |
| a failure only CI's Linux shows | `mise run browser --linux` with a leg (`layout-chromium-1`) or test labels |

CI runs the layout gate in two legs side by side (`browser:layout-1`,
`browser:layout-2`) and runs them in Chromium as well
(`browser:layout-chromium-1`, `-2`), which `mise run browser` leaves to CI.

`browser --linux` draws in Chromium unless `--engine` names another. It runs
on this checkout inside the Playwright image `ci.yml` gives the browser job, as
linux/amd64 with the job's options, set up as the toolchain action sets a
runner up. A failed rule's screenshots land in `var/browser-failures/`, as on
the Mac.

The VM it starts runs x64 through Rosetta, and emulated, the browser draws
slower than on a runner. So a rule that reads a page against its frames fails
more often than in CI: a whole leg's classes side by side fail far past CI's
list, and two classes named as labels come closer to it.

What the browser gates read from the environment:

| Variable | Set by | Does |
| :--- | :--- | :--- |
| `HQ_BROWSER_ENGINE` | development | the browser gates' engine: `webkit` by default, `chromium` or `firefox` |
| `HQ_BROWSER_CHANNEL` | development | a Playwright browser channel for that engine |
| `HQ_BROWSER_FACE` | development | `fallback` draws the browser gates with Inter withheld, in the stack's next face (`--fallback-face`) |

#### Policy, fonts and stalls

Every page a gate serves runs under the policy HQ sends. A page rendered
through the test client keeps its response's headers (`gate.answered`), the
API reference its own derived policy, and a page a test puts together HQ's.

A policy refusal, a console error or an uncaught script error fails the test,
naming the page: a refusal its directive and what it refused, an uncaught
error the width, its message and the frame it was thrown from. The example
estate fails page by page, every other gate at the end of its test.

The gates wait for Inter, but a reader whose copy is late is drawn in whatever
face the platform has (`font-display: optional`), and each platform sets text
at its own widths. `--fallback-face` on a layout leg, `browser:navigation` and
`dev:look` (`HQ_BROWSER_FACE=fallback` for a `mise run test` label) withholds
Inter's face. Every page is then drawn in the next face of the stylesheet's
stack (`--font`), the platform's system face, and a layout that holds only at
Inter's widths fails.

A test that stalls fails and says where. Each test process (every parallel
worker) watches for progress: a test, a class fixture or a subtest starting, a
subtest ending, a page in `dev:look`.

Once none has come for `HQ_TEST_STALL_SECONDS` (300 unless the environment
says otherwise; `0` turns it off, for a debugger), it prints a stall report.
The report holds what it was running, every thread's and greenlet's stack, and
in a browser gate the end of Playwright's own log (the browser's launch and output, each
call and navigation). Then the test or fixture fails with `Stalled` and the
run goes on.

One that does not stop within 30 seconds of that ends its process, and a
parallel worker that ends stops the whole run. So a hang never holds the heavy
turn or a CI job until its bound.

#### Looking at a page

`mise run dev:look <pages>...` is the same browser for looking rather than
gating. `mise run dev:look --help` gives its pages, its flags and the commands
to copy. What follows is how a look behaves, which the help does not say.

A record's page is found in the estate the seed builds, and a record's address
that seed does not hold is refused with the ones it does. Composed, the
showcase and standard seeds hold each extension's demo too, and `all` reads
the record pages it names ([Plugins](Plugins.md#demo-data)). A bare word is the
one page of the estate whose path it ends (`contacts`), and the run's lines name that
page as `estate/<path>`; a word that ends several paths or none is refused with
the nearest. An unknown page, rule, probe or seed is refused in seconds, before the look
waits for the heavy turn; a record's page, named by its route or a template,
is checked once the run has built the estate. Without `--composed`, a
template no host directory holds, or a route under a namespace the host lacks,
is refused as an extension's page, with the flag that loads them. A
page that answers anything but 200 fails the run. A query rides on the page's name as it does on an address
(`'search/?q=docker'`, `'control_plane:containers?q=hq'`), on any page the
server answers, so a page drawn from its query (Command Center's results, a
filtered list) is a name like any other.

A component catalog entry (`part/notice-error`, `table/figures`, every key of
`CATALOG` in `hq/platform/core/tests/browser/catalog.py`) is drawn by the
catalog gate's own `CatalogLayoutTests.render`, so a look and that gate never
draw an entry apart. It is answered from memory as a gate page is, with no
server or estate behind it, so a look at one entry is the quick way to see a
change to a shared part, ahead of the catalog's whole browser class. A name that begins entries
(`part/notice`, `table`) is each of them, in the catalog's order. The gate
reads the catalog without script, so a look draws an entry without it too, in
a context of its own beside a real page's, which keeps script unless
`--no-script`. A page drawn without script runs no frame or timer, so the
look waits on none there: `--eval`, `--css` and `--open` read it as it stands.
The catalog builds its entries as it is imported, so a look reads it only
for `all` or a name no other page is: a `--base` whose parts' constructors this
checkout's catalog does not fit still looks at every other page.

A form's refused state is looked at by name, `post:<name>`, one of `POSTS` in
`hq/platform/core/tests/look_posts.py`: the estate's owner sends that form to
the seed's estate once, as its page sends it, and the page is what the refusal
answers, its errors drawn. What it sends is written there or read from the
estate, never typed, and the page must answer the refusal's status: one the
form accepts fails the run, as does a page that answers anything but 200. A new
refused state to look at is one entry there; `test_look_names` sends every one
and holds it refused.

A look saves a full-page screenshot of each page at each width in a folder of
the run's own, `var/look/<when it began>/`, which its last line names. A page
taller than the window is also shot as window-sized bands
(`<page>-<width>.band-01.png` on), whatever the shot: a page whose height
never holds still is cut from one reading of it, and its line says so, as is a
`.failed.png`.

A run removes the folders older than a day beyond the newest `KEPT_RUNS` of
them (`hq/platform/core/tests/look.py`) as it starts, so an earlier run's shots
stay where it printed them. A page that could not be looked at (refused, no
`main`, a rule that could not run) is shot as it stands, as
`<page>-<width>.failed.png`.

The look keeps its test database in `var/dev-look/` through Django's
`--keepdb`, so a second run creates nothing; an edited migration needs
`rm -r var/dev-look`. A composed look keeps its database in
`var/dev-look/composed/`.

A look holds still: the page's clock is frozen at the moment the run began
(its `--base` run shares that instant), and each screenshot is taken with
animations finished. A file that changes in the checkout while the run draws
(any file git does not ignore) fails it, since its shots and verdict would mix
two trees.

`--operation 'GET /api/v2/...'` on `api/docs` opens that one operation before
the shot: its tag, and its body and every response with each property in
them. The shot's line names the band files that show it.

`--open` opens disclosures round after round until nothing more opens, then
shows one popover of each kind on its own, shot as
`<page>-<width>.popover-<id>.png`. A kind is the popover's classes and the
table and column it sits in, so a list's many panels from one partial cost one
run.

With `--expand` an open row holds the columns at the widths they had before it
opened (`tables.js`), and `--columns` measures a held table's min and max on a
copy without the hold. `--dialog` presses a control that opens the dialog,
after any menu holding it, so the page's own wiring opens it. A dialog fills
itself from its region's own request (`X-Fragment`), which only a server
answers, so `--dialog` signs in to the seed's estate behind any page, a gate
page answered from memory too, and a region of another page is asked of it.

`--deep` asks every linked page marked as a prefetch, so a page that would act
or reach out is refused and reported instead.

`--rule survey` runs every check the gates hold (`BrowserGate.findings`), so
what it finds on a page the gates read is what they fail on. Markup a vendored
script draws (`data-vendored` on its mount, `SELECTORS["vendored"]`: the API
reference's viewer) is styled by its own rules alone, and held by its own
tests instead. With `--open-folds`, a page with a fold closed is surveyed
again with every fold open, a fold within a fold and one a region brings in
included (`survey.open_every_fold`), and only what that adds is said, under
"with every fold open"; the folds close again before a dialog opens.

A run that judges takes no screenshots, since a shot and its bands cost more
than the rule that reads the page: `--shots` takes them too, and `--base`,
`--eval` and `--operation`, which read them, take them anyway. What a rule
finds is written in the run's folder (`<page>-<width>.txt`) as the run goes, so a run stopped part way keeps it, and to `report.txt` there
once the run ends. Console errors, uncaught script errors and policy refusals
are judged as findings are: without a base any fails the run, against one only
a new one does.

`--base <ref>` looks at the same pages at `<ref>` first, in a temporary
worktree that runs the base's own look, catalog, fixtures and seeds, with only
this checkout's probes laid over it, so both are judged by this checkout's
rules. A probe laid over that does not import there is put back as the base
has it, and the closing line names it. A page or catalog entry the base cannot
draw, such as one added here or a record its estate lacks, is said there,
judged here alone, every finding on it new, and named in the closing line. The base's
screenshots are kept in
`base/` inside the run's folder, under the same names as this checkout's
beside them, so the two sides of a change can be compared once the worktree is
gone and after later runs.

Findings are matched as a multiset by page, width, check and a key. The key
takes out every UUID, hex id and origin panel's id, every number other than a
size or contrast, and the record named after a measure; each size and
contrast is kept to its power of four. So a finding whose row number moved,
whose overflow grew by a few pixels, or that names another record is neither
new nor fixed, and one grown many times over is new.

With `--columns`, `--eval` or `--probe`, each line the base measured otherwise
is printed under this checkout's, marked with the ref.

`--probe <module>:<NAME>` runs one probe of
`hq/platform/core/tests/browser/probes/` on each shot, as the survey runs it,
with `--arg`'s JSON as its argument (the selector the survey passes it, from
`SELECTORS`). Its findings print a line each beside the shot and are kept as
`.probe.txt`, so a probe is tried on a page before it is wired into the
survey.

`--css` lays a stylesheet over every page the look loads, before the shot,
`--eval`, `--probe` and the rule. It is adopted as a constructed sheet, which
the page's content security policy does not govern; lifting the policy
instead would hide the refusals the run reports. The base is drawn without
it, so `--base HEAD --css <file> --rule survey` reports what a candidate fix
fixes and what it breaks before any sheet in `static/css` is edited.

`--eval` takes a file holding a function, called with nothing, or one
expression. What it returns prints under the page and width (as JSON unless
it is text) and is kept beside the screenshot as `.eval.txt`. An element in
it, at any depth of arrays and plain objects, prints as its tag, id and
classes and the shot file that shows it: the band (`.band-NN.png`) whose part
of the page holds its box, or "not shown". The page settles after the script,
so the screenshot draws what it changed. A computed style, or an element's
chain of ancestors with their widths:

```js
() => {
  const chain = [];
  for (let el = document.querySelector('.table-scroll td'); el; el = el.parentElement) {
    chain.push(`${el.localName}.${[...el.classList].join('.')} ${Math.round(el.getBoundingClientRect().width)}`);
  }
  return chain;
}
```

#### Measuring performance

`mise run perf <pages>...` measures how light and fast HQ is, page by page;
`mise tasks info perf` gives its flags. It is a report, not a gate. It
measures:

- on the server: time, queries, the rows its tables hold and its HTML size;
- in Chromium: bytes, requests, first and largest contentful paint (first
  visit and repeat), script time and layout shift;
- memory and cold start.

A first load's bytes are also shown by type (document, stylesheet, script,
font, image). A load whose stylesheets and scripts all came uncompressed, as a
live server sends them, shows no byte figure.

A page asked that answers anything but 200 fails the run, except one the
`--base` answers 404 and this tree serves: perf says it is not at the base and
measures it on this side alone, outside the count of pages compared. The
stress sets live in the test tree, so no server can serve them to `--seed`. Like `dev:look`,
perf refuses a page or data set it cannot serve, with the nearest names, before
it waits for the heavy turn.

Each run is kept as `var/perf/<run>.json`, saved after each measurement. A
failed one is recorded with its error and the rest go on, and each step says
how long it took. `<run>.txt`, which the session log shows, is the summary,
and `<run>.table` has every page.

Memory is what the server allocated: a scratch server's footprint, or an
image's cgroup `anon`, the same reading `release:report` takes of production,
so the three compare. A container's `memory.current`, `docker stats` and a
process's RSS add clean pages of the files it mapped or read (the shared
libraries, the database), charged to whichever container read them first.

Production holds more than a pass over the pages does: the bridge and the
signing-key keeper it runs, and the MCP server. The MCP server starts on an
agent's first call and keeps its SDK's protocol models and the tool contract
loaded from then on.

`--image` measures the image production runs, composed as Compose builds a
pull request's candidate.

`--base` keeps both servers up and asks every page of both in turn over
`--requests` rounds, alternating which goes first, before anything else
touches either server. Each number is shown beside how far it moved, and each
page's server time beside its median round-by-round difference and that
difference's interquartile range. A page whose difference is past that range
is marked moved, and the closing line counts those pages.

Unless `--fresh`, the base's browser, memory and cold start come from a
complete kept report under the same plan, seed included. That report must
have been measured under a one-minute load average within 2 of the load the
tip is measured under; a report that kept no load stands in for nothing. The
closing line names both loads beside the run the base is kept from.

A ref that refuses the extension set for its plugin API makes the run measure
the host alone on both sides, as `--host-alone` asks, and its closing line
says so.

With `--base`, one page takes about 40 s (under 30 with the base kept) and
every page about two and a half minutes (two with the base kept).
`mise run perf:note <label> <value> [unit]` keeps a measurement made by hand
for the session log.

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

### Tracing

A request, the capability it runs and an automation worker's pass can be
read as traces in otel-tui, a terminal viewer pinned in `mise.toml`. The
OpenTelemetry SDK and its OTLP exporter are in the `dev` group and never in
the image, and nothing of them loads unless `OTEL_EXPORTER_OTLP_ENDPOINT` is
set. Start the viewer on loopback in one terminal, then the server with the
endpoint in another:

```sh
otel-tui --host 127.0.0.1
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 mise run dev:scratch
```

The same variable on `mise run smoke:runner` sends the worker's pass. A web
or MCP request is one trace whose id is its request id, the operation id of
every audit row it writes, with a span for each capability it runs
(`hq/platform/core/tracing.py`). A worker's pass is a trace of its own: its
claim to its report, with each task outcome and lease renewal as an event
(`hq_runner/tracing.py`). No span carries who acted.

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
controller/               Go: the controller, hq-host, hq-secrets and hq-edge
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
Every mise task that runs Django sets the key and names the database: the
scratch tasks this checkout's `var/dev/scratch/`, and each gate
`SEVERINO_SANDBOX` in place of a path, so each process it runs makes a run
folder of its own, as a test run does (`hq/config/sandbox.py`), with a new
empty database there, and removes it as it ends. The image names its own.
So `manage.py` runs as `mise run manage -- <command>` or
`mise run manage --scratch -- <command>`.

Media, exports and static files default to `var/media`, `var/exports` and
`var/static`, a test run's and a gate's media and exports to its run folder;
`SEVERINO_MEDIA_ROOT`, `SEVERINO_EXPORTS_ROOT` and `DJANGO_STATIC_ROOT`
override them.

To move a database, stop the application, back it up with its WAL contents,
and check the copy before pointing HQ at it.

### Extension environments

`uv sync` manages the host environment exactly, so never run it over an
environment that holds extension wheels: it removes what the host lock does
not name. Export runtime dependencies with `uv export --locked
--no-default-groups --no-emit-project` and install the hashed export
additively instead. The composed suite installs nothing; it imports the
extensions' sources through `PYTHONPATH`.
