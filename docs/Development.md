# Development

Setup, the gates, and where things live. Read [`AGENTS.md`](../AGENTS.md)
first: it is the engineering contract, with the architecture map, placement
rules, the host/extension boundary and the definition of done.
How agents work on HQ through these gates: [AI](AI.md).

## Setup

Install [mise](https://mise.jdx.dev) and a `python3`; `mise install` brings uv
with the other tools `fast` and `check` run.

A tool only a heavier gate needs (CodeQL, age) is `lazy` in `mise.toml`: its
first use installs it, checked against `mise.lock` like the rest. The audit
sandbox refuses that install, so an `audit:` task names each lazy tool it runs
to `setup:sandbox` (`depends = ["setup:sandbox ripgrep"]`), which installs it
before the sandbox opens.

Go is the one tool `mise.toml` does not pin itself. It reads the release from
`controller/go.mod`'s `toolchain` line, which Renovate moves, so mise loads
`mise.toml` only beside that file.

```sh
git clone https://github.com/joeseverino/severino-hq.git
cd severino-hq
mise install
mise run dev:scratch --seed showcase
```

The log ends on an `Open:` link at `http://localhost:<port>`, which signs a
browser in as the throwaway account; without `--seed`, the line before it
says the database is empty. Nothing reads a `.env` file:
production's configuration is in [Deployment](Deployment.md#configuration).

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

A kept file the tests import a name from that its copy at the ref lacks is a
fixture the new test needs, never the fix, so it is laid over again for a
further run, and the line names it. When a run still does not import or
build, the kept files hold a fixture nothing names, which proves nothing:
`--keep` only what holds the fix. A shell suite's support is the fakes it
reads, so its pass runs once.

A test that drives `at.main` patches its `ROOT`, as `TestAtRefTests` does,
since `changed_tests` lists files with git.

### Audits

The `audit:` tasks measure without writing anything tracked, so a read-only
auditor may run any of them. mise runs each in its OS sandbox (the `audit`
template), where a write outside the temporary and Go build cache paths
fails; `audit:memory` also writes `var/perf`. `audit:image`, `audit:mcp`
and `audit:css-usage` write as they measure, so they run outside it. A `.venv` out of sync with
`uv.lock` fails an audit, since uv cannot sync it there: run `uv sync`.

| Task | What it answers | Example |
| --- | --- | --- |
| `audit:ruff <rules> [paths]...` | Ruff's findings, counted per rule | `mise run audit:ruff SLF001 '!**/tests/**'` |
| `audit:ast [globs]...` | per file, the docstrings over 8 lines and the `getattr` calls of a literal name with a default (`tools/audit/ast-grep.yml`) | `mise run audit:ast 'hq/**'` |
| `audit:dead` | the names `hq/` and `hq_sdk/` define that nothing outside tests reads (a test-support module's, `testing.py` or `*_testing.py`, that nothing reads at all), as vulture finds them, with what it cannot see handed to it as uses (`[tool.vulture]`, `tools/lint/vulture-whitelist.py`) | `mise run audit:dead --fields` |
| `audit:layers [importer] [imported]` | the imports between core, application and domains, counted per pair both ways, or one pair's listed by module (grimp) | `mise run audit:layers core domains` |
| `audit:imports <path>` | the files that import one, string imports included | `mise run audit:imports hq/platform/core/static.py` |
| `audit:churn <since>` | each tracked source file's commits since a date, most first, generated and vendored files left out (`.gitattributes`); reach for it before a refactor or split | `mise run audit:churn 2026-10-01` |
| `audit:loc [--base <ref>]` | the working tree's lines per language and per area (host code, Python and Go tests, templates, CSS, scripts, Go controller, tools) as scc counts them, generated and vendored files and what git ignores left out; with `--base`, each row that moved since the ref, read through `git archive`, and the total; reach for it to state a landing's net lines | `mise run audit:loc --base origin/main` |
| `audit:go [linters]` | golangci-lint's findings, counted per linter | `mise run audit:go nilerr,gocritic` |
| `audit:go-dead` | the controller's functions no main reaches on Linux, as deadcode finds them (a test-support package's that nothing reaches at all) | `mise run audit:go-dead` |
| `audit:memory` | each module's own share of Python memory at boot and after a pass over the navigation, extensions composed; `--after` adds the paths boot never takes, each with its import times (`python -X importtime`); it ends by naming the `var/perf` JSON that holds every row its tables leave out | `mise run audit:memory --host-alone --after oidc,mcp-call,outbound` |
| `audit:advisories` | `lint:audit`'s own report, run as a read: uv audit's advisories and adverse statuses per package, osv-scanner's table of each advisory over every tracked lockfile, and mise audit's of the pinned tools (`tools/pipeline/audit.sh`); needs the network | `mise run audit:advisories` |
| `audit:dependency-weight` | each locked Python distribution's installed size (its RECORD, through `importlib.metadata`), runtime apart from the dev groups, and each package's and module's bytes in every shipped Go binary (go-size-analyzer) | `mise run audit:dependency-weight` |
| `audit:image [image]` | a built image's layers (size, instruction, the parts of HQ each holds), what a container sees by part, site-packages' largest, and dive's wasted bytes | `mise run audit:image severino-hq:ci-local` |
| `audit:ci-timings <run>...` | where CI runs' time went, each job's end lag included | `mise run audit:ci-timings 41175916328` |
| `audit:docs-readability` | each doc's Flesch-Kincaid grade, prose words, and longest paragraph and sentence, longest paragraph first (`tools/lint/vale/audit.ini`) | `mise run audit:docs-readability` |
| `audit:bcd [feature]...` | each browser's first supporting version, from browser-compat-data, Safari first, and whether the browser floor (docs/Design.md, Browsers) has it; with none, every place the stylesheet writes a feature newer than the floor | `mise run audit:bcd css.properties.container-type` |
| `audit:supports <engine> <condition>...` | what the browser gates' own WebKit and Chromium answer to `CSS.supports`, one line per engine per condition; reach for it over `audit:bcd` to know what the gates draw, since their WebKit is ahead of every Safari release | `mise run audit:supports gates 'selector(:heading)' 'color: alpha(from red / 50%)'` |
| `audit:css-usage [pages]...` | each app stylesheet's bytes and the bytes of the rules no page `dev:look` draws matches, read in Chromium at every width in both themes with everything opened, then each unused rule as file:line: dead (a class or data attribute nothing writes), unreached (a state no seed raises) and apart (a media query, feature test or selector that never held); takes the heavy turn. Reach for it before and after any CSS cut, and in a UI pass's inventory | `mise run audit:css-usage all` |

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
| `dev:scratch --runner` | one with the automation worker beside it, to walk Playbooks end to end ([The automation worker beside a development server](#the-automation-worker-beside-a-development-server)) |
| `dev` | the long-lived server behind a TLS proxy, configured by the gitignored `dev.local.toml` (copy `tools/dev/dev.local.example.toml`), with the automation worker always beside it |

Every one collects versioned assets and runs Uvicorn with reload on the
production ASGI path, through `tools/dev/serve.py`, which answers a loopback
host on both 127.0.0.1 and ::1, so `localhost` loads in every browser. It
watches this checkout and each extension's sources through watchfiles (the
`dev` group): a Python file or a template restarts it. Nothing under `.claude/worktrees`, `.venv` or `var/` does, so another
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

The scratch server's `Open:` link signs in its throwaway account
(`hq/platform/core/dev_sign_in.py`, as Jupyter's token URL does). It works
once and expires after its `LIFETIME`, and each worker a reload starts
prints a new one. The account's password is in the log above, for a second
browser. The link is routed only on a scratch server with DEBUG on and bound
to loopback, and answers a caller on loopback alone.

Every loopback server shares the browser's cookies for one host name whatever
the port, so signing in to a second one at `localhost` signs the first out:
open the second's link with `127.0.0.1` in place of `localhost`.

### The automation worker beside a development server

`mise run dev` always serves the bridge and runs the real automation worker
(`hq_runner`) beside it, and `mise run dev:scratch --seed showcase --runner`
does the same for a scratch server. A scan HQ asks for, once a reading of
the repositories finds the source's branch moved, is then claimed, its commit
can be adopted, and a run is claimed, run by ansible-playbook and reported
with each task's outcome, on the machine page and in Jobs. Without
`--runner`, a scratch server's scan or run stays queued.

The server serves the bridge as the image does (`SEVERINO_BRIDGE_SOCKET`,
and the worker's `SEVERINO_AUTOMATION_BRIDGE_SOCKET` beside it, which the worker
is given), on a socket in a private directory under `$TMPDIR`, one per database, since the
database's directory can be too long a path for one. `tools/dev/scratch_automation.py` stands
where the host's automation units stand: on each automation doorbell, or 15
minutes after the last worker ended, it writes a connections document for
the machine the server runs on and starts the worker from the runner group's
environment (`var/runner/venv`, which `smoke:runner` shares, with the
collections `hq_runner/collections.yml` pins installed in it as the image
installs them, by `tools/lib/runner-environment.sh`). It watches the
reloader's own process, so a reload leaves it running and starts no second
one, and each worker it starts runs the code as it stands then. It stops with
the server and removes the socket's directory.

What it holds to:

- The bridge is a Unix socket only this account reaches; the web server
  listens where it did.
- The worker gets its `HQ_AUTOMATION_*` variables and nothing of HQ's
  environment, and no token or credential.
- Its repository is `tools/dev/scratch_playbooks/`, committed to a repository
  in an `automation/` directory beside the database at each start and fetched
  over git's file transport, whatever the database adopted before.
- Its one login is to the machine HQ's inventory says the server runs on
  (the machine page's "runs HQ", matched by this host's addresses), so
  Playbooks offers that machine alone and a run's page names where it ran.
  A scratch server with no such machine declares `scratch-host` at this
  host's addresses in its own database, through the operator's create
  command, when the worker first starts; `dev` declares nothing in its real
  data, and with no such machine its worker has no login. The login reaches
  127.0.0.1 and every play there sets its connection to local, so a run acts
  on this Mac alone.
- Its one change, `example.note_write`, writes one file in the run's own
  temporary directory, which the worker removes as the run ends; where it
  writes is no input's to say.

What it cannot show: GitHub's word on a commit's signature, so the worker
reports each commit it scans as verified; the renderer's real runner
connections and identities; and the egress table and container confinement the
host gives the worker. A run that names a commit the local repository does not
hold, as the showcase's do, is ended as failed to fetch by the first pass that
finds it waiting, and a database that adopted another commit shows the local
one as pending. Adopting the local commit makes `example.disk_report`,
`example.container_report` (a read through a shipped collection's module) and
`example.note_write` its playbooks.

An agent's path is walked on the same server with `hq_call --agent <client>
--grant <permissions>`, one MCP tool call made as that agent, the request on
stdin as `hq_call` takes it:

```sh
echo '{"tool": "execute_capability", "arguments": {"name": "automation.example.note_write.run", "payload": {"hosts": ["scratch-host"], "parameters": {"text": "hello"}, "idempotency_key": "walk-1"}}}' \
  | mise run manage --scratch -- hq_call --agent example-claude --grant read,manage_infrastructure
```

It answers only with DEBUG on and every host the server serves a loopback one,
as a scratch server's are. The agent holds the permissions named and no more,
and a name no agent's token can carry is refused. The call is admitted and
served as an agent's over /mcp/ (its rate, the Agents page's brakes, its
authorization and approval), so the change above answers `awaiting_approval`
until a person approves it.

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
every leg, and the job's summary lists it, so a new top-level path is placed in
the sources of each leg that reads it. Scan,
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
(`tools/gate/verdict.py`). It names each failed task and test and the one
command that reruns just those. The tasks, their times and each failed task's
last lines are mise's own: the report of each run of mise a step starts
(`mise run --report-file`) and the output mise logs of each task
(`mise tasks log`), so a task is named as the run a step started names it. A gate whose steps queued for the
[heavy turn](#heavy-gates-take-turns) says how long beside its total
(`passed in 664.5s, 410s of it waiting for the turn`), so the rest reads as
working time.

The whole verdict is kept in `var/gate/<name>.txt` beside the run before it,
with each failed test's exception and traceback, each failed task's last
lines, and under the step times what each run said it spent (the test
runner's slowest classes, and each measured line below), so where a gate's
time went is read from the file. All the run printed is in
`var/gate/<name>.log` (`HQ_GATE_VERDICTS` names another folder, as a gate
tool's test does for every process it starts). A gate's first line names both files, since the head of a long
output is the part a reader shown only an excerpt of it always sees.

What a gate prints is short enough to read whole. Its commands' output goes
to the log; the screen gets the gate's own lines and, on a failure, each
failed task's last lines and each failed test's traceback from its last frame
in the test's own code, then the verdict line, which names the rerun. A
terminal and a CI job (`CI` set), whose job log is the only one kept there,
are shown all of it as it runs too; `HQ_GATE_OUTPUT=all` or `short` says
which anywhere. Follow a run in progress with `tail -f` on its log.

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
one that found anything or judged nothing fails. A `--base` look is a survey
too, so its verdict replaces the record and names only the pages it was asked.

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
| `mise run lint:prose` | about six seconds; under a second on one file |
| `mise run lint` | about a minute |
| `mise run fast` | under a minute |

`mise run fast [BASE]` maps the changed files against a base. One asked for
(`BASE`, `$CHECK_BASE`) is read as its merge base with HEAD. With none, the
base is the nearest commit on HEAD's first-parent history (as far back as
`PASSED_REACH` in `tools/gate/chain.py`) whose tree passed fast (the record
below), so the change is only what came after a proven tree. Without one,
it is the merge base with the branch HEAD lands on, then `origin/main`,
`main`. The branch a tree lands on is one answer, `landing` in
`tools/gate/landing.py`, which the composed set reads too. A branch with an
upstream is a pull request's head, so it lands as itself, as CI reads it.
One without, as a worktree's branch is, lands on its own name where that is
among the branches asked about, else on the nearest of them (fewest commits
of its own off it, then fewest it lacks), the one a worktree was made from; a
tie or a detached tree lands nowhere. Here the branches asked about are those
tracking a remote one. The line that counts the selection names the base and
why it is the one. `--list` prints the selection.

Before anything judges the tree, each selected quick gate whose tool fixes
what it finds runs its fix mode (`--fix` in its usage: `lint:ruff` with
Ruff's fixes and format, `lint:css` with Stylelint's, `lint:js` with
ESLint's, `lint:mise-format` with `mise fmt`, `lint:ast-grep` with each
rule's `fix:`, `lint:controller` with gofmt, `go fix` and `go generate`, and
each quick `lint:drift-*` gate with its file's writer), and the run names the files
they rewrote. The run then judges the tree the fixes left, and its verdict
line ends by naming the files they rewrote, which the checkout holds
uncommitted: a commit of them is the tree the run judged, the one a landing
compares with the commit. A run sent to a second machine has its fixes run
here first and sends the tree they leave. What a tool cannot fix, its gate
still reports. Only fast and `mise run fix` pass `--fix`, so CI's gates only
check.

The tests it selects are:

- when the only Python modules an edit starts from are test modules under
  `tests/` with no importers, fast runs those modules directly. Tests that
  read the changed files are still selected. Editing a shared test fixture
  or base class in a module another imports takes the regular path below;
- the test modules that ran a function body the change reaches, as the
  record of what each test ran says. `mise run fast:map` makes it: the suite
  fast runs, under coverage.py's C tracer, each class's lines kept under its
  module's name (a dynamic context), in coverage's own database, kept under
  the common git directory for every worktree and named by the tree it
  measured (`tools/gate/ran.py`). It takes about three minutes; run it after
  a landing, or when fast's line says many files changed since its tree. A
  change reaches the bodies it edits, and the bodies and the top-level
  definitions (a function's defaults and decorators, a class's body, a
  constant, an import) that refer to a definition it edits as its module
  loads, all the way through those definitions. A method's signature is part
  of its body; a method added to a class reaches the bodies of its other
  methods; a type a function's signature names reaches the function only
  when a decorator of this tree takes it, since only something that reads
  the signature sees it. A body reaches no further
  than the tests that ran it, since a test that runs what calls it runs it
  too, except to what calls it as a module loads (`COUNTS = (unread(),)`)
  and, for a body a cache keeps (`functools.cache`) or one the record says
  ran as the suite started, before any test (a registry's assembly), to all
  that refers to it. The edit is read from the changed modules' text at the base and at the
  record's tree, so what a test runs that its record predates is a change
  too, and a comment or the module's docstring changes nothing. A change
  with no Python module, migration or task definition starts from the files
  its readers name and the routes they render, so it skips the coverage record. The
  references are read from the source: a name an import binds, an attribute
  read off an imported module (`inbox.recent`), a name the module defines,
  and in a module under a `tests` directory a dotted string (a mock target).
  A module used as a value (`mock.patch.object(inbox, ...)`) refers to every
  name in it. This is pytest-testmon's model. A body in a module the record
  does not hold (a test, a helper under `tests`, a module new since), and a
  test module the record does not hold or that holds a test tagged `source`
  (which reads files, and which the record's run leaves out), take the reach
  read from the source
  alone, a body reaching every definition that refers to it, as every body
  does when there is no record;
- the test modules that load a changed module, where a name cannot carry
  the change: a statement that binds nothing
  changed or was reached, a decorator of this tree takes a reached
  definition (it may register it), nothing else refers to a reached
  definition (Django, a registry or a string reaches it), a module was
  deleted, a migration changed its app's models, or a task definition
  changed. These walk the imports a module runs as it loads (at its top
  level or in a class body, never in a function, a `lazy` import or one
  under `TYPE_CHECKING`), as grimp, import-linter's graph, reads them, all
  the way. They also walk any import, those in functions and lazy ones too,
  directly or through one module between, since further out those close a
  cycle through the shared layers that reaches most of the suite. A test
  module and a URL module never take this walk: the runner runs the one, and
  the routes reach through the other;
- every test that names a route whose view the change reaches, by its name
  (`reverse("control_plane:detail")`) or by a path it answers
  (`"/infrastructure/topology/"`, an f-string's field filling a converter),
  each path resolved to the first route it fits, as Django resolves it: a test
  that drives a view through the client imports its models, never its views.
  A route is read from each URL module's `path()` calls, its prefix from the
  call that mounts its module (`Mount("infrastructure/", "<module>")`,
  `include()`) or from `ROOT_URLCONF`. A view is reached when the change
  reaches its definition by name, when its module is walked as it loads,
  when its module imports a module that names a
  changed file, or, for the module that names the file itself, when the view
  (or a class it derives from) is the definition that names it. A template
  reaches through every template that includes or extends it, and a tag
  library through every template that loads it;
- every test that names an app whose models the change reaches, by a model
  label (`"projects.Project"`) or its app label; a migration reaches its app's
  models;
- every test that reads the tasks, when a task definition or a file a task's
  run names changed;
- every module whose source names a changed file, and every test that
  imports one: a file that is not Python by its path or a directory above
  it, as one string (`".github/workflows"`) or as segments (`ROOT /
  ".github" / "workflows"`), from the root or from a directory above the
  module (`Path(__file__).parent / "report.jq"`), a directory of one part
  only joined to a path (`BASE_DIR / "templates"`), and a file under a
  `templates` or `static` directory by the name Django loads it by
  (`"partials/table.html"`) too; a Python module by its
  whole path, and a test alone by a directory of two parts or more above it.
  A test that reads a workflow through a helper naming the workflows'
  directory is one;
A test tagged `tree` finds its files by walking this checkout, or loads what a
fresh process starts with (the startup budgets), so whatever the change, it
reads the whole tree: fast's runs leave it out (`--exclude-tag tree`), and
CI's `lint:source` runs it. So does the composed suite with the extensions,
which a pull request's Composition job runs.

A run runs its selection however wide it is. `--whole` runs the whole suite
(the tests tagged `tree` aside) with every quick gate instead, whatever
changed, and its pass records the tree.

Before the tests it runs each quick gate the change affects, as mise's
`--affected` reads it from the base: a quick gate is a task tagged
`fast`, and it is affected when a changed or deleted file matches its
`sources` (`!` patterns take paths out) or its own definition changed. Every
quick gate's sources also name the tool pins and task definitions, and each
one that runs Python its dependencies (`pyproject.toml`, `uv.lock`), so a
change to one runs every gate that reads it. The controller's Go tests
(`test:controller`) are a quick gate too, which runs beside the tests in
the heavy turn, as it takes the lock itself, and Go's own test cache reruns
only the packages a change reached. Each quick gate declares
`outputs = []`, so a direct `mise run` of it never skips as up to date. The
shell gate also runs when a path the change touched is one
`tools/gate/shell-sources.sh` names (a deleted one as the base had it), which
no glob can say. `mise run --tag fast --affected --affected-base <ref>
--affected-json` names each gate it would pick and why, running none. A lint gate that reaches the network, or whose check
fast makes another way, is left untagged, and so is one whose input is the
whole tree whatever changed: the `lint:drift-*` gates a Django command
writes, `lint:deploycheck`, `lint:deptry`,
`lint:sdk-breaking`, `lint:structural` and `test:migrations`. CI's Lint and
Test jobs and `mise run check` run them. `lint:mypy` stays, since mypy's
cache rechecks only a changed module and those whose imports' interfaces
changed, and so does `lint:imports`, which reads the import graph in about a
second whenever a Python file changes.

A gate whose linter reads one file at a time (`lint:ruff`, `lint:ast-grep`,
`lint:spelling`, `lint:prose`, `lint:sentence-ends`, `lint:refusal-text`, `lint:file-rules`,
`lint:source-scans`) runs through hk, as `hk.pkl` declares it, and
fast hands it the files the change touched: its step reads those its glob
matches, and its `-whole` step reads the whole tree when one of them is a file
that step names (the tool's config, the pins). Run alone, with no files, as
the Lint job runs it, it reads the whole tree. A check that reads the whole
tree to judge part of it is a step too: `lint:refusal-text` indexes every
module and holds those it is handed and the modules that import one.

A change to a stylesheet under `static/css/app/` or a template selects
`fast:look`, which runs in the heavy turn beside the tests and holds the
component catalog entries the change reaches to every check the catalog's
sweeps hold, in WebKit at 320 and 1280 pixels, through `mise run dev:look
--rule survey`, once with script and once with `--no-script`. A template reaches each entry whose page draws it;
a sheet reaches each entry whose markup holds a class or data attribute named
by a rule a changed line lies in (`hq/platform/core/tests/look_reach.py`), and
every entry when such a rule names neither (an element, `:root`, a token).
`tools/gate/look_entries.py` names each file's reach in the look's output,
and leaves out a file that reaches every entry: that look is the whole
catalog, CI's Browser job, as is any look of a Python-only change.

A change to what the extensions' contract check reads (a template under
`templates/partials/`, the template contract and its tag libraries, plugin
validation, the file rules, `hq_sdk`, the check itself and the pins) selects
`fast:extensions`, which runs in the heavy turn beside the tests: the check
(`plugin:check`) of each extension package this checkout composes, as
`tools/lib/extension-set.sh` picks the set, side by side, each line
naming its package and how long it took. Where none is composed it says so
and passes.

The generated API cases (`test:api-properties`) stay CI's Test job and
`mise run check`'s: every change Django loads reaches every operation, and
Schemathesis sends each operation's boundary and refusal cases whatever the
example budget.

fast runs in two stages, each one `mise run`. The quick gates run first, side
by side, before the tests are selected, and the tests start only once every
one has passed, so a lint or drift finding fails the run in the seconds they
take. A recording waits for the selection, which says whether it runs. The tests then run
beside each selected gate that runs in the heavy turn ([Heavy gates take
turns](#heavy-gates-take-turns)), in one share of the turn sized by what they
run, and the share tells every task under it that it holds the turn, as mise
hands a lock down, so a gate that takes the lock itself goes ahead at once,
beside the rest. Both runs' output is kept in
`var/gate/fast.log` ([The gates](#the-gates)), and both run in the config
environment `mise.fast.toml` sets: mise's summary output, where a gate that
passed prints its `passed in` line as it ends and one that failed prints
whole as it ends. The tests run as `fast:tests` (`mise-tasks/fast/`), whose
own output setting outranks the environment's, so they print as they run,
their wait for the heavy turn included; `--output` or `MISE_TASK_OUTPUT`
would outrank both.

Each run goes on past a failure (`--continue-on-error`) until every task it
holds has ended, so it names every gate and test that failed, and the
verdict's last line names the rerun of each. The selected tests that read every browser gate module (each
imports `browser_modules`: the probe check among them) start no browser,
and run first as `fast:source`, ahead of the tests, which run after them
whether or not they passed.

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
with no record here is asked of GitHub once, through `gh`: when CI's last
completed run on that commit (`ci.yml`, a run of this repository's own
commit, never a fork's) passed, its tree is recorded first, so a run from
`origin/main` or any pushed commit starts the chain, and each run after reads
from it. With no network, no `gh` or no passed run, the run says so in its one
line on the record and goes on unrecorded (`passed_in_ci` in
`tools/gate/chain.py`).

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
settings both ways. The suite inside an image runs once, in the pull
request's Composition job, on the composed image production runs: its release
promotes that image ([Deployment](Deployment.md#checked-once)).

`mise run check --release` then checks the deploy host, read-only, on the
committed `HEAD`. Its
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
  walk takes and answers JSON. `django_printers` is one.

Each walks the tree, an ast-grep command line included, so the test carries
the `tree` tag.

The wording rules are Vale's, in `tools/lint/vale`: `.vale.ini` there says
which files each style reads, `styles` holds the rules, and `lint:prose` runs
them. Each rule swaps one word or character for another; a wording rule that
would ask for a rewrite is a line in [Design](Design.md#rules), never a check.
`hq/platform/core/plain_words.py` hands Vale the template attributes and tag
literals it cannot read. `uv run python -m hq.platform.core.plain_words
<paths>` prints every finding in those paths at its line, on the text and the
attributes; `vale --output=line` alone sees only the text. `mise run
audit:prose-strings` prints every line of text the rules read, at its file
and line (`--by-file` counts them).

A finding names its rule and what to say instead. A word that must stand in
one file is turned off for that file in that `.vale.ini`, with the reason
beside it.

### Derived files

A file generated from a source is declared once, by the `lint:drift-*` gate
that holds it, in `mise-tasks/lint.toml`: the gate is tagged `derived`, and its
`vars.writes` names each file its fix mode writes whole. `mise tasks -x
--hidden --tag derived` lists them. Adding
one is its writer and that gate; everything else reads the declaration:

- `mise run fix` (`fix:derived`) runs every derived gate's fix mode, then
  `go generate` for the Go types the controller's contracts feed, which
  `lint:controller` holds and declares in its own `vars.writes`;
- `.gitattributes` marks each declared file `linguist-generated`, written by
  `lint:drift-gitattributes` (a hand-written attribute goes in a
  `.gitattributes` in its own directory);
- `tools/tests/test_drift_gates.py` fails on a drift gate that neither
  declares its files nor records itself, and on a declared file that is not
  in the checkout.

`mise run fix` then formats as the lint gates read. It also lowers each count
in `tools/lint/eslint-suppressions.json` that has fallen to what ESLint finds (`fix:suppressions`), so a change that removes a
suppressed problem leaves its count right. A count that rose is a new problem:
`fix` leaves it, and `lint:js` refuses it.

Each `lint:drift-*` gate checks one against its source without writing. Its
failure names the command that rewrites that file alone, in seconds; `fast`
runs those tagged `fast` a change reaches, and `mise run fix` writes them all.
A rebase conflict in one is settled by
`mise run fix`, never by hand.

### The extension set

`mise run setup:extensions <checkout>...`, run once per machine in the main
checkout, names the set in the gitignored `mise.local.toml`. It reads each
package of each checkout as admission reads it: `HQ_LOCAL_PYTHONPATH` lists
their `src` directories and `HQ_LOCAL_PLUGINS` the manifests they declare.
mise writes the file and keeps every other value in it, and
`mise doctor project` then checks that the set imports.

A worktree Claude Code makes gets a copy of the file, and of mypy's cache,
through `.worktreeinclude`, so it composes the extensions' main checkouts.
A value of either variable already in the environment wins. So an extension
worktree is composed by naming its sources on the command line, one `src` per
package, joined by `:`, and another set by naming its manifests:

```sh
HQ_LOCAL_PYTHONPATH=<worktree>/packages/<name>/src mise run test --composed
HQ_LOCAL_PLUGINS=tests.fixtures.example_hq_plugin.plugin:plugin mise run dev:scratch
```

Every step that composes the set (the composed suite, the dev servers and
`manage --scratch`, perf and its base, `dev:look --composed` and its base,
`image:compose`, `audit:mcp`, `audit:memory`, `audit:registry --composed` and
`wire-pocket-id`) loads the set as one host tree can load it, picked in one
place, `pick` in `tools/dev/extension_set.py`. A file task reaches it through
`tools/lib/extension-set.sh`. A host tree lands where `landing` says (What
`fast` runs), asked about the branches a named checkout's repository carries
and its default. Where it lands on one of them other than the default, that
branch is composed, as CI composes a coordinated change:
the worktree checked out on it as it stands, else the branch's commit,
unpacked under `var/extensions/`. So a worktree of a host branch composes the
extensions' branch of the same name whatever `mise.local.toml` names. Each
other package is asked the plugin API its manifest requires, beside the
`PLUGIN_API_VERSION` of the host tree or commit the step runs:

- every package requires it: the trees named are composed as they stand;
- a repository's packages require another: its newest commit on HEAD whose
  manifests all require the host's API, made before the host moved on from
  the commit it stands on, is composed instead, unpacked once per commit
  under `var/extensions/` (an extension's later commit at the same API may use
  what the host added since);
- no commit does: no extension is composed, and the step runs the host alone.

The step's first line says which: the `src` each package is imported from, the
commit picked and why, or why none is composed.

`mise run test --composed` runs the suite as the image does: in a copy of only
the files `Dockerfile.dockerignore` admits, less what the Dockerfile's
`COPY --exclude` keeps out of `/app`, with `DEBUG` off, `SEVERINO_HQ_SOURCE`
stamped and only the runtime dependencies (`.venv-image`). A test that reads a
file the image leaves out, or loads a development tool, fails here.

Without a set it runs the host alone and says so.

That copy is read from the two files by `tools/lib/image-tree.sh`.
`image:verify:tree`, one of the checks `mise run build` runs, holds it to the
built image's `/app`, so a form the reading misses fails the build naming each
path.

`mise run build` also builds the automation worker's image, the Dockerfile's
`runner` stage (`--runner-tag`, `severino-hq-runner:ci-local` by default), and
`image:verify:runner` checks it: the account it runs as, the programs a run
reaches, its entry point, and `mise run smoke:runner --image <tag>`, the real
playbook run inside it under the worker's confinement. The checks read the
host image from `IMAGE` and the worker's from `RUNNER_IMAGE`; a host image
handed over needs its worker image handed over beside it.

`--cache <ref>` reads a registry layer cache, and the worker's build reads its
own beside it at `<ref>-runner` as well as the host's. `--cache-write` writes
both, the worker's to its own ref, since an export replaces what its ref held.
Only main's Build cache job passes it.

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
the machine takes a share of its budget sized by what it runs, shared by every
checkout and worktree, and shares run side by side up to the budget. Only a
step that needs the whole machine whatever it is given (an image build, the
browser gates, CodeQL) takes the turn to itself. A waiting task names the
holder.

A step of one process that holds little (a Django command, a linter) runs
beside the holder. A gate whose one process still loads the machine
through most of a fast run (`lint:mypy` is one) carries the tag `heavy`, which only fast reads: fast
runs it beside its tests in the share it takes for them, never among its
quick gates, while a direct `mise run` of it, `lint` and CI run it as a step
of one process. An aggregate (`check`, `lint`, `test`, `browser`) holds no
turn of its own, so its light gates never wait; `build` holds one across its
steps, which share the VM it boots. A one-off command in the built image
(`mise run image:run -- python -V`) holds the turn and the VM while it runs,
so another checkout's stop leaves the VM up under it.

The turn is mise's task lock `heavy`, a name every checkout and worktree on
the machine shares. A TOML task takes it by extending the `heavy` template in
`mise.toml` (`django-browser`), a file task with `#MISE lock=["gate", "heavy"]` in its
header and the line `. ./tools/lib/heavy-turn.sh` before its work; either
declares a `timeout`, which mise starts once the task holds the lock, so
heavy tasks asked together (`mise run browser:webkit :::
browser:demo`) queue on the turn and each runs in full, and a hung holder
is stopped. A heavy task started inside a turn runs at once, since mise hands
the lock down (`MISE_TASK_LOCKS`), and a holder killed outright frees it.
`tests/shell/test-heavy.sh` holds the turn's promises.

A landing goes first. Every task that takes the turn passes the lock `gate`
first, which mise takes in sorted order, so a task waiting there holds
nothing. `mise run landing -- <command>` holds the gate for the command's
lifetime: no other checkout starts a heavy run until the landing ends, and
the landing waits behind only the run already in the turn, since it takes the
gate with a blocking lock that the kernel hands over as the holder ends, where
mise polls. The command's own heavy tasks inherit the gate
(`MISE_TASK_LOCKS`) and take turns on `heavy` among themselves. A share of
fewer than every token still passes a landing's gate when its tokens are free
now, as EASY backfill starts a small job on idle nodes ahead of the reserved
one: a narrow test run starts during the landing's quick gates, beside a
share of the landing's that leaves it room, or while the landing runs on the
second machine. It takes its tokens behind the door, where the landing's own
share collects every token it waits for, so no backfill passes it. A
landing marks its hold with a lock of its own, `gate.landing`, taken once it
holds the gate and let go by the kernel as it ends, so a whole run waiting at
the gate lets none through.

A share's measured cost lets it start before tokens free up. The measurer
records the peak each share's tree held, keyed by its task and arguments
(`peak_file`), so every worktree reads the same record. A share of fewer than
every token that waits for tokens starts on none once that peak fits the
memory the OS has available now: macOS's memorystatus level (what
`memory_pressure` reports free), Linux's `MemAvailable`. It still takes
`heavy` shared, so never beside a whole run, and passes a closed gate only
when a landing closes it. A narrow run then starts beside a landing's share of
every token. A run that needs every token never does, since its memory may lie
outside its tree (a Docker VM). A run with no record waits for its tokens.
The peak is sampled every two seconds, so a run shorter than that records
less than it held.

A run of tests takes a token for each process it runs tests in
(`tools/gate/turn.py`): one when it is serial (`--parallel 1`), and
otherwise as many as the workers Django starts, one for each test class up to
the workers asked for, two where a browser draws beside one. A run asking
past the budget takes all of it, so a small machine runs no more at once than
it holds. So `mise run test <module>.<Class>` takes one, and a run across the
cores (a module of many classes, the suite) takes every token. The decision
weighs the arguments the run passes: a task hands it its run's own options
before the labels (`test_arguments`).
`test --at` takes what its labels weigh here, since the ref's runs of them
come one at a time; `test --go` and the shell suites take every token.
fast's tests and its `heavy` gates take one share: what the tests weigh, one
token for each such gate, and every token beside a gate that takes the lock.
`dev:look` declares its share (`turn_share`): a browser's two tokens for each
process it splits its pages among, one for each twenty pages up to what the
machine holds, or perf's when `--base` or `--perf` runs perf after it and it
draws more. perf takes what it draws: two for this tree's server and the
Chromium that reads it, one more for a base's server beside it, and every
token with `--image`, whose Docker VM holds memory of its own; its page bytes
and query counts do not move with load, and its times fail nothing. A task
whose weight depends on what it is given (`test`, `fast`'s tests,
`lint:source`, the composed suite, `dev:look`, `perf`, `browser --linux`)
takes its share through `python -m tools.gate.turn --share`
(`tools/lib/heavy-turn.sh` for a file task, `turn_share=all` for every token),
under the gate's own bound: `mise run --timeout` would count the wait too.

A share counts in tokens out of the machine's budget (`CAPACITY`), each about
a core and 600 MB: a process that runs tests takes one, one beside which a
browser draws (a look, a survey) two. The budget is derived from the machine,
a token for each core and no more than one for each 2 GB, so two surveys or
four test runs fit together beside the dev server and a browser on an 8 GB
Mac, and a 12-core, 32 GB machine holds twelve. That count takes every core
as equal, so a machine whose cores are slow or throttled declares the count
measured on it instead, once, in its clone's git config
(`git config hq.gateTokens <tokens>`), which `tools/gate/room.py` reads
before the fallback. mise's task lock pools take a count fixed where the task
declares it, all or none with no queue
([task_lock.rs](https://github.com/jdx/mise/blob/v2026.11.11/src/task/task_lock.rs)),
so they hold neither a share weighed from its run nor a share of several
waiting among shares of one. A share is the counting semaphore the kernel's flock(2) makes, as
`flock -s` and GNU make's jobserver tokens do: it takes the lock `heavy` shared,
which a whole run's exclusive lock waits for and keeps out, and its tokens
from four slot files beside it, one share at a time behind a door, so a
share of two is never starved by shares of one. A whole run waiting for the
turn holds the gate while it waits (mise takes `gate`, then polls `heavy`),
and a share passes the gate before it takes its tokens, so a waiting whole
run lets the shares it waits for end and lets no new one start: no run
starves. A run inside a share, like one inside the turn, goes ahead at once:
a share tells its command its tokens (`HQ_TURN_SHARE`) and that it holds
`gate` and `heavy` as mise hands a lock down (`MISE_TASK_LOCKS`), so a task
under it that takes the lock in its header runs inside it rather than waiting
on it. A share waits, names who holds what it waits for and gives up after
`MISE_TASK_LOCK_TIMEOUT` as mise's own waits do, and a gate's closing line
counts its wait, as it counts mise's.

A task that waits for the turn without holding the lock (each of those, and
an aggregate whose script runs `mise run` on one that waits) declares
`SEVERINO_WAITS_FOR_HEAVY_TURN="1"` in its env, the one place
`mise tasks ls --json` shows it without running anything; mise lists no edge
for a `mise run` inside a script. A tool outside the repository, such as an
agent's command hook, can read it beside the lock.

A waiting task names the holder, its checkout, how long it has held the turn
and its pid, again every 30 seconds:

```text
[lint:shell] waiting for lock heavy, held by test:suite in ~/hq (pid 4121, for 3m02s)
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
sources and a share each run their command under it, and `HeavyTaskTests`
holds every task that takes the lock to one of the first two. A
heavy run inside a measured one adds no line of its own, so measuring one
label is that one command.

The line is a report, never a check: load and the mix of tests move both
memory and time, so nothing fails on them, and the command's exit status is
the run's whatever the measuring did, unless it stopped a run of tests that
made no progress ([Policy, fonts and stalls](#policy-fonts-and-stalls)). A measurement it cannot take (no
`footprint`, a sample that failed) is named in the line in its place.

### Gates on a second machine

A second machine you reach over SSH can take the gates a person types:
`fast`, `test` (the composed suite and `--at` included), `dev:look`,
`browser` and `perf`. One setting in the repository's git config, which every
worktree shares however it was made, turns it on:

```sh
git config hq.gateRemote gates:hq
```

It names the machine as git names a repository over SSH, the host then
HQ's checkout there. Each of those tasks then starts on whichever machine
has the tokens it takes free now, with nothing new to type. The task weighs
its run first, by the same line the machine that runs it reads again
(`turn_share`, before it sources `tools/lib/remote.sh`); a run it does not
weigh (fast, the composed suite, `browser`) takes every token. The sender
reads both machines' free tokens and capacity with one function,
`tools/gate/room.py`, here in process and there through the host half's
`room` step, and sends the run there when that machine has room for it, else
keeps it here when this one has, else waits on the machine that lacks the
fewest tokens. A weight of this machine's whole budget is the whole of the
other machine's too. One sender chooses at a time, and each run it sent holds
its tokens until its share there holds them, so runs started together
spread across both machines. A run there runs under that machine's own
heavy turn, never taking this one's. Its output reads as a run here, paths
named in this checkout, after one line naming where it runs:

```text
[fast] runs on gates, tree 928ba11e2
```

A run kept here or left waiting says why, with each machine's free tokens:

```text
[dev:look] runs here: it takes 2 tokens, and gates has 0 of 6 tokens free
```

A machine that does not answer within five seconds leaves the run here, after
one line saying so; so does a run inside a turn here, one whose flag acts on
this machine (`--open`, `--docs`, `browser --linux`, `perf --image`), and one
typed with `HQ_GATE_REMOTE=` before it. A task the release check runs that
reads a secret or the deploy host (`failure-logs`, `mint`, the host check)
never asks.

How a run goes (`tools/gate/remote.py`, and `tools/gate/remote-host.sh` there):

1. The tree to judge, committed or not, is the one `var/gate/` names
   (`tools/gate/tree.py`). It is pushed with git over SSH as a commit on
   HEAD, or HEAD itself when it holds the tree, with each commit an argument
   names (fast's base, `--at`), which are resolved here: a branch there is
   not this one. The push skips the repository's pre-push hook, which guards
   a push to its forge, since these refs publish nothing. fast's default base is resolved here too, from this
   checkout's chain of passed trees, which is also synced there,
   so it selects what it would select here. A run that may compose (the
   composed suite, fast, perf, and a look that runs perf) picks the set here,
   as a run here would, and sends the extension tree it picked the same way,
   to the clone of the same name as the checkout `HQ_LOCAL_PYTHONPATH` names,
   beside HQ's there, with `HQ_LOCAL_PLUGINS`. The tree sent composes it there
   as it stands, never picking again; one that cannot send it runs here.
2. There, the run takes the first free worktree of this checkout's under
   `~/.cache/hq-gates` (the checkout is keyed by its machine and path), held
   with `flock` until the run ends, so two runs at once never share one and
   each tree a checkout sends finds the environment and caches the last one
   warmed. The worktree is reset to the commit sent, everything an earlier
   run left that git tracks or does not ignore removed, and the pushed refs
   are deleted. Its HEAD is then this checkout's HEAD, with the rest of the
   tree as uncommitted work, so a step that reads HEAD (a look's
   `catalog:<sheet>`) reads the change it would read here. A new one holds the files `.worktreeinclude` names as that
   checkout has them. A file under `var/` that an argument names (a `--eval`
   probe) is staged before the run and laid in. A worktree untouched for a
   day is removed as the next run starts.
3. The same `mise run <task> <arguments>` runs in it, the arguments as mise
   parsed them here, with each variable of this run that changes what a gate
   does (`CARRIED`, such as `HQ_BROWSER_ENGINE`) and this run's output mode,
   since the run there writes to no terminal. Every other variable a gate
   reads is named in `LEFT` with why the run there reads its own. Closing the
   connection stops it.
   The run only checks, as CI's gates do: fast's fixes ran here before the
   tree was sent, its line naming what they rewrote follows the verdict, and a
   run that leaves the worktree holding any tree but the one sent fails,
   naming each file it changed.
4. What it wrote comes back, archived there as the run ends, while the
   worktree is still its own: each `var/gate/` verdict that names the tree
   sent (any other is left there, and said so), the logs, `var/look/` and
   `var/perf/`.
   A pass fast recorded there is recorded here only then, its file naming the
   machine it ran on.

One SSH connection carries every step (`ControlMaster`), with no agent or
display forwarded. The machine needs mise, `flock` (util-linux), a `python3`
for its room and the
checkouts (the extension
clone for fast and the composed suite), and nothing that writes to GitHub or reaches
production; a run of browser tests installs the engine it draws in.

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
- in the Chromium leg, each page's Cumulative Layout Shift stays within 0.1.

`setup:lint-tools` installs the axe-core `tools/lint/package.json` pins before
every browser gate. Taste (evenness, placement, row height) is written in
[Design](Design.md#held-by-eye) and judged by eye.

A failed subtest saves a screenshot of its page as it failed, named for its
page, width and rule (`example-two-402-overflow.png`). It goes in a
`<test>-*` folder of `var/browser-failures/<task>/`, which the next browser run
of that task replaces; a test that fails outside its subtests saves `page.png`
there.

Beside each screenshot is a Playwright trace of the same name (`.zip`): what
the failed subtest did, each step with the page as it stood.
`uv run --group browser playwright show-trace <file>` opens it. CI uploads
every such folder.

A layout defect fails once, however many classes, engines, widths and
shares found it (`hq/platform/core/verdicts.py`). A subtest whose check finds
something records each finding with where it was found (the engine and the
system it drew on, the class, the page, the width and the state) and the Part that draws what it
names: the Part the check measures, else the Part owning the first class the
finding names. The run then fails once for each defect, a rule broken where
one Part draws (or, where no Part does, findings alike but for their
numbers): its rule, its Part, that Part's file (the one place to fix), each
finding with every place, and the `dev:look` that finds it again, run as
printed. CI's shares write their findings to `HQ_BROWSER_FINDINGS` instead, and its
Browser verdict job fails once per defect over every leg's. A `dev:look`
that fails says its findings the same way. After the slowest classes, the
run prints two ledgers: each check's time in the page, and each rule's time
per subtest, each row with where it was slowest. They fail nothing.

A check is declared once, beside its probe: an entry of its probe module's
`CHECKS` (`hq/platform/core/tests/browser/probes/__init__.py` says what an
entry holds), which the survey collects from every module there, so a new check lands as
its `CHECKS` entry, never an edit to `survey.py`. The entry carries its own
examples, markup it must find (`plants`, each with the findings it expects)
and markup it must pass (`clear`), and one test walks them all in each engine
(`hq/platform/core/tests/browser/plants.py`), so a check needs no test of its
own.

A check selects markup only through `SELECTORS`, handing a selector to the
page through `css` (`hq/platform/core/tests/browser/selectors.py`), which
fails where the selector matches nothing on a page the check asks it of,
naming the part it belongs to and the fix. A selector some pages lack (one a
sweep reads on every page) is declared `optional`, and is then held only on
its part's catalog entry.

A browser test that fails only sometimes is a race a reader can meet.
Reproduce it by taking the reader's steps back to back, which opens the race
where CPU load does not, and wait on the state the page reaches when it
settles, never on a longer timer.

#### The two estates

The crowded estate is the example estate with crowding laid on top
(`crowd()` in `hq/platform/core/tests/crowding.py`): long names, many rows,
held requests and long histories, added in the test tree so the demo never
loads them. Its pages are `PAGES` in
`hq/platform/core/tests/test_browser_fixtures.py`.

Every list page is also filled from its own model
(`hq/platform/core/tests/test_browser_derived_fixtures.py`). Each view drawn
through `ListPageMixin` is found in the URLconf, and its rows carry every
field at its length, money at the largest a household records, and the
longest label of every choice. So a new list or field is measured at its worst
the day it exists (`dev:look derived/<app>/<name>` shows one).

The example estate (`hq/platform/core/example_estate/`, the homelab the demo
and `manage.py seed showcase` show) is read by
`hq.platform.core.tests.browser.estate` on every page the navigation declares.
It is read with script on and deferred parts loaded, at 402, 820 and 1360
pixels and, on a page that draws a table, at `WIDE_TABLES`, in the light
palette at each and the dark at 402 and 1360 (in
Chromium at 402 and 1360 only). A finding names its check, page, width and
theme.

A list's tables are also read at every shape of the layout
(`a_table_fits_its_box_at_every_width`): from 320 pixels up, at each edge
of the stylesheet's viewport queries and wherever a box around a table
changes shape or its table lets a rank go between two, every row a reader
can open is opened and every table check is read, with what opening the rows
moved. A finding names its check and the first width it was found at.

`WIDE_TABLES` is read from the stylesheet: the narrowest window where a
table's region, on the page and in a card, has passed the breakpoint its wide
rules need. So the columns that keep their line are measured with the least
room; the crowded estate is held there too.

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
| the legs named | `mise run browser <leg>...` (`webkit`, `catalog`, `demo`) |
| one CI share of a leg | `HQ_BROWSER_SHARD=2/7 mise run browser:webkit` |
| one browser test | `mise run test <label>`, which installs the engine first |
| a failure only CI's Linux shows | `mise run browser --linux` with a leg (`chromium`) or test labels |

`browser:webkit` runs every browser test but the catalog walk in WebKit,
which judges, since HQ is read in Safari. Chromium is a second opinion,
`browser:chromium`, which `mise run browser` leaves to CI: the catalog of
shared parts, the example estate at a phone and a laptop, and the layout
shifts only Chromium records. Every test it runs is also run in WebKit.

An engine draws in its system's fonts, and macOS's monospace face is wider
than Linux's, so a part can fit in CI's Linux legs and wrap in Safari. So the
catalog walk (the classes tagged `catalog`) runs once per engine: in WebKit
on a macOS runner (`browser:catalog`, WebKit unless `HQ_BROWSER_ENGINE` names
another), its findings joining the other legs' in Browser verdict, and in
Chromium on Linux (`browser:chromium`). On a Mac, `mise run browser` runs
`browser:catalog` in the Mac's own fonts.

CI splits each engine's gate into shares that run side by side, `i/n` in
the browser jobs' matrices. A sweep marked `@sweep` runs in every share and reads
its share of the pages (`BrowserGate.mine`); any other test runs in the one
share its id picks (`hq/platform/core/tests/browser/shards.py`). The shares
together run the whole gate, which GateReferenceTests holds.

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
| `HQ_BROWSER_SHARD` | CI's browser matrices | `i/n` runs one share of a browser gate; unset, the whole gate |

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
at its own widths. `--fallback-face` on `browser:webkit` and
`dev:look` (`HQ_BROWSER_FACE=fallback` for a `mise run test` label) withholds
Inter's face. Every page is then drawn in the next face of the stylesheet's
stack (`--font`), the platform's system face, and a layout that holds only at
Inter's widths fails.

A test that stalls fails and says where. Each test process (every parallel
worker) watches for progress: a test, a class fixture or a subtest starting, a
subtest ending, a page in `dev:look`, or CPU spent by the process and every
process under it, a browser included. A tree under 1% of a core is idle; one
at work spends more however loaded the machine is, so load never stalls a
test.

Once none has come for `HQ_TEST_STALL_SECONDS` (300 unless the environment
says otherwise; `0` turns it off, for a debugger), it prints a stall report.
The report holds what it was running, every thread's and greenlet's stack, and
in a browser gate the end of Playwright's own log (the browser's launch and output, each
call and navigation). Then the test or fixture fails with `Stalled` and the
run goes on.

One that does not stop within 30 seconds of that ends its process, and a
parallel worker that ends stops the whole run. So a hang never holds the heavy
turn or a CI job until its bound. A loop that spins (a renderer inside
`page.evaluate`) spends CPU, so the watch keeps it; its job's
`timeout-minutes` bounds it on CI.

A hang outside every test process's watch (a worker that never starts, a
parent waiting on a pipe, a process stuck on its way out) is the measurer's
(`tools/lib/measured`, which runs every heavy gate). Once its tree holds a
test run, a test beginning in any process (each mark is left in the folder
`HQ_TEST_MARKS` names) or the tree spending 1% of a core is progress. With
neither for twice `HQ_TEST_STALL_SECONDS`, so a test's own watch fails it
first, it names the test each process was in, has each dump its stacks
(faulthandler, on `SIGUSR1`), stops the whole tree and exits 124, which
frees the heavy turn:

```text
heavy: no test began and the run spent under 1% of a core for 10m00s, so it is stopped; hung in example.tests.Hung.test_forever (pid 4121, for 10m02s)
```

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
is checked once the run has built the estate. Either refusal exits 64 and
ends on the command that runs the look again with each refused name swapped
for its nearest, the flags as given. Without `--composed`, a
template no host directory holds, or a route under a namespace the host lacks,
is refused as an extension's page, with the flag that loads them. A
page that answers anything but 200 fails the run. A query rides on the page's name as it does on an address
(`'search/?q=docker'`, `'control_plane:containers?q=hq'`), on any page the
server answers, so a page drawn from its query (Command Center's results, a
filtered list) is a name like any other.

A look of many pages (`all`) is split among processes, each with its own
browser and a copy of the test database, its pages dealt as CI deals a
browser gate's sweep (`hq/platform/core/tests/look_shares.py`). Each says
the page it is on as it starts, after its share (`share 2/4:`); every page's
lines then follow in the order asked, as one process prints them, and a
line names how many processes looked. A share that ends early fails the
run, naming its pages. `--deep` stays one process.

A component catalog entry (`part/notice-error`, `table/figures`, every key of
`CATALOG`, which `hq/platform/core/tests/browser/catalog/` gathers from each
`Part`'s examples and its families' modules) is drawn by the
catalog gate's own `catalog.page`, so a look and that gate never
draw an entry apart. `gallery` is every entry on one page, each in a region
named for it under its name, after a map of its families, each part in a
shadow root of its own, so no entry reaches another: the gate holds the
page around them to the survey. HQ's System > Design page draws the same
gallery as its content, drawn by a checkout's server from the catalog it runs on each request, and
read from the image build's copy in the image. An entry is answered from memory as a gate page is, with no
server or estate behind it, so a look at one entry is the quick way to see a
change to a shared part, ahead of the catalog's whole browser class. A name that begins entries
(`part/notice`, `table`) is each of them, in the catalog's order. An
entry's page loads HQ's scripts as base.html does (`scripts/page.html`), and
the gate reads it twice, as the server drew it and once its scripts have run,
so a look draws an entry with script, as any page, unless `--no-script`. A
page drawn without script runs no frame or timer, so the look waits on none
there: `--eval`, `--css` and `--open` read it as it stands.
The catalog builds its entries as it is imported, so a look reads it only
for `all` or a name no other page is: a `--base` whose parts' constructors this
checkout's catalog does not fit still looks at every other page.

A form's refused state is looked at by name, `post:<name>`, one of `POSTS` in
`hq/platform/core/tests/look_posts.py`: the estate's owner sends that form to
the seed's estate once, as its page sends it, and the page is what the refusal
answers, its errors drawn. What it sends is written there or read from the
estate, never typed, and the page must answer the refusal's status: one the
form accepts fails the run, as does a page that answers anything but 200. A new
refused state to look at is one entry there.

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
`rm -r var/dev-look`. Each run works on a copy of its own and hands it back
as it ends, so looks started together share no database. A composed look
keeps its database in `var/dev-look/composed/`.

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
reference's viewer) is styled by its own rules alone, and the gates read
none of it. It reads each page as its gate does (`Judged` in
`hq/platform/core/tests/browser/widths.py`, which the sweeps read too): at
its widths, `WIDE_TABLES` among them on a page that draws a table, and where
the gate opens a page (a catalog entry, a list, a page without script),
again with every fold open, a fold within a fold and one a region brings in
included (`survey.open_every_fold`), and with script every row a reader
can open opened by its own toggle, saying only what that adds under "with
every fold open", and with each anchored popover shown at the widths the
gate shows one. `--open-folds` opens a page with script the same way. The
folds close again before a dialog opens. Where the gate reads a page's
tables at every window width (a catalog entry and a dense page with script),
the survey does too, from the first width it looks at, under "at every
width".

A run that judges takes no screenshots, since a shot and its bands cost more
than the rule that reads the page: `--shots` takes them too, and `--base`,
`--eval` and `--operation`, which read them, take them anyway. What a rule
finds is written in the run's folder (`<page>-<width>.txt`) as the run goes, so a run stopped part way keeps it, and to `report.txt` there
once the run ends; a run that fails prints each page's first finding, its
check named first. Console errors, uncaught script errors and policy refusals
are judged as findings are: without a base any fails the run, against one only
a new one does. A file a page or the look can read (`SURVEY_INPUTS` in
`hq/platform/core/tests/look.py`, with the look's own code) that changes
while the run draws is named in its last line with the tree it began on: what
it found stands, and the gate keeps no pass of a tree that moved (`tree -`).

`--base <ref>` looks at the same pages at `<ref>` first, in a temporary
worktree that runs the base's own look, catalog, fixtures and seeds, with only
this checkout's probes laid over it, so both are judged by this checkout's
rules. A probe laid over that does not import there is put back as the base
has it, or taken out where the base has none, and should the base's own look
still not import, every probe is the base's own; the closing line names them.
A base that cannot be looked at even so (its look does not import or does not
finish) is said in one line, and the run judges without a base, every finding
counting. A page or catalog entry the base cannot
draw, such as one added here or a record its estate lacks or does not answer, is said there,
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

- on the server: time, the rows its tables hold, and its queries and HTML
  split into what the page costs once and what it costs per row;
- in Chromium: bytes, requests, first and largest contentful paint (first
  visit and repeat), script time and layout shift;
- memory and cold start.

A page's queries are `page q`, the first of each SQL statement it sends, and
`row q`, every repeat of one (N+1), which grows with the rows it draws. Its
HTML is `page KB`, outside its records' rows, and `B/row`, their bytes per
record. Each ref is measured on its own seed, so a page that drew more rows at
the same cost moves only `rows`, `row q` and its total HTML.

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

Memory is what the server allocated: a scratch server's footprint on macOS
and its proportional set size with its swapped share on Linux, the readings
the heavy gates' peak memory takes, or an image's cgroup `anon`, the same
reading `release:report` takes of production. A container's
`memory.current`, `docker stats` and a process's RSS add clean pages of the
files it mapped or read (the shared libraries, the database), charged to
whichever container read them first. The title names the machine measured,
and a run kept on another machine never stands in for a base.

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

Each side composes the extension set its own commit can load
([The extension set](#the-extension-set)): a base at another plugin API
composes the extensions' newest commit at that API. A base that can load no
commit of them, or whose start fails where its HQ cannot load its set but
loads the host alone (`manage.py check` run both ways), such as an import an
extension needs that the base lacks, makes the run measure the host alone on
both sides, as `--host-alone` asks, so the extensions' cost is never counted
as the change's. Its closing line says why.

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
(`hq/platform/core/tracing.py`). Work it queues carries that trace on its
record as a W3C `traceparent`, as OpenTelemetry's messaging conventions
attach a creation context to a message, and the claim and report that take
it up continue it: a press, the merge it queued and the Apply after it are one
trace, as they are one chain in the audit log (`AuditLog.trace`). A job a
press asks for and a read it asks the controller for carry it the same way,
and the job's rows and what the sweep that answers the read found join the
chain. A worker's pass is a trace of its own: its
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
worker/                   TypeScript: the Cloudflare Worker that verifies providers' webhooks and sends their names to the host's listener (lint:worker, test:worker; worker:deploy deploys it, [Security](Security.md#the-inbound-events-worker))
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
