# HQ engineering contract

The shortest path from a fresh checkout to a safe change, for people and
agents alike. Setup, tasks and layout: [Development](docs/Development.md).

## Start here

1. Run `git status --short` and keep any unrelated work. Before designing,
   `git fetch` and compare with the branch your work lands on (`origin/main`
   by default): the mechanism you need may already exist there.
2. Done, for a piece and for the branch it lands on, is the
   [definition of done](#definition-of-done).

mise prints the tasks live, so this file does not list them:

| To see | Run |
| --- | --- |
| the verbs to start from, each with when to reach for it | `mise tasks` |
| the steps they run, each runnable by the name a gate's output gives it | `mise tasks --hidden` |
| what a task takes, without running it | `mise tasks info <task>` |
| the examples a task carries (`dev:look`'s) | `mise run <task> --help` |

What the listing does not say:

- `fast` checks only the files a change touched and runs the tests it
  reaches through imports, names and the paths a module names. A gate or test
  that reads the whole tree (the derived files, the structural bar, a test
  tagged `tree`) and the composed suite are CI's and `check`'s.
- A new test is proven to fail without the fix by `mise run test --at <base>
  <labels>`, never by a base worktree, snapshot or checkout made by hand.
- In a worktree of your own, try a change on `dev:scratch`, a throwaway
  server, never on `dev`, the main checkout's server on real data.

A CI job is named for the task it runs (`lint`, `test`, `browser`, `build`,
`scan`), so a gate goes in its job's file under `mise-tasks/`
(`mise-tasks/lint.toml` for `lint:*`), never in a workflow.

The composed suite (`mise run test --composed`) runs the host and its
extensions together, as the image runs them. CI cannot run it, since it
needs the extensions' checkouts. It loads the set the gitignored
`mise.local.toml` names. A worktree Claude Code makes gets a copy of that
file through `.worktreeinclude`, so it composes the extensions' main
checkouts. To compose an extension worktree instead, set
[one variable on the command line](docs/Development.md#the-extension-set).

A test that reads the repository's files carries `@tag("source")`, since no
image ships those files. It runs only in `lint:source` (`mise run lint:source
<labels>` for some). One that walks a tree also carries `tree`. The
suite runs in parallel on WAL SQLite through
`hq/platform/core/file_clones.py`: read it before changing the test database. Adding a host domain: [New Domain](docs/New-Domain.md). How agents
build and operate HQ: [AI and HQ](docs/AI.md).

## Architecture in one minute

- `hq/platform/application/` owns use cases, authorization, transactions,
  projections, capability execution, and plugin internals.
- Django apps own persistence and domain-specific models. Adapters may query
  for rendering; they do not mutate models directly.
- Web, CLI, MCP, and HTTP API are delivery adapters over the same application
  behavior. Never reimplement a business rule in an adapter.
- `hq_sdk/` is the only supported Python import surface for plugins.
- `templates/partials/`, `hq/platform/application/ui.py`,
  `hq/platform/application/tables.py`, and the matching `hq_sdk.*` modules are
  the shared frontend contract.
- The machine API's current contract is `/api/v2/`. Writes are capability
  authorized, schema validated and transactionally audited.

Detail: [Architecture](docs/Architecture.md), [Plugins](docs/Plugins.md),
[API](docs/API.md).

## The host does not know its extensions

HQ is a host. Its extensions are separate packages with their own
repositories, tests and release cycles, installed at composition time. This
repository names none of them: not their inventory, repository
identifiers, routes, models, fixtures or vocabulary. Examples and tests use
the synthetic `example.*` namespace.

A host that names an extension has taken a dependency on it, and loses what
this design exists for:

- an extension is added without touching the host;
- the host runs with none installed;
- the two are developed on independent schedules.

The real installed set reaches the host only through composition metadata
supplied at runtime. Two tests in
`hq/platform/application/tests/test_plugins.py` keep it that way, and one that
fails has found a coupling.

Generic integration policy belongs here; domain meaning belongs in its own
package. An abstraction with one domain-specific caller stays in that domain
until a genuine shared contract appears.

## Where changes belong

| Change | Owner |
| --- | --- |
| Business rule or mutation | application service in its domain |
| HTTP, CLI, MCP, or view parsing/rendering | adapter calling that service |
| Plugin-facing primitive | implementation in HQ plus export from `hq_sdk` |
| Repeated layout or interaction | shared partial/CSS/JS primitive |
| Plugin identity or domain semantics | the extension, in its own repository |
| Cross-plugin compatibility | generic composition check in HQ |

A provider is named once, in `ADMITTED`
(`hq/domains/control_plane/provider_adapters/__init__.py`). Everything else
derives from its adapter's `DEFINITIONS`, `CONNECTIONS` and `OBSERVATIONS`
(its readings module's), and `test_admission.py` there holds that to be enough. Its Go half in
`controller/providers/` is handed its connection as a typed value
(`r.Supplied.For`), never a setting by name
([Providers](docs/Architecture.md#providers)).

Tests live in each package's `tests/` directory, and what one arranges (a
person, a signed-in client, a declared resource, a provider connection, the
standard principals, a drawn template) comes from `hq/platform/core/testing.py`. When a definition moves,
retarget every `mock.patch` that named its old home, since a patch of a name
no longer looked up there patches nothing. A check that walks a path (an
architecture test, a glob) fails when its path matches nothing.

## Rules that eliminate bug classes

- A new mechanism states its security properties in its design. What a
  resource can do is derived from topology facts, never from a per-kind flag.
- Never interpolate a newline-separated file list into `eval` or `bash -c`,
  where each line runs as a command: split it with `set --` and pass `"$@"`.
- Reject unknown input: a capability's command is a frozen dataclass, and
  `CapabilitySpec` refuses one that is open.
- Enforce authorization in the shared capability/view layer, not ad hoc in a
  template or handler.
- A rule one row must keep on its own is a `Rule` in the model's
  `Meta.constraints` (`hq/platform/core/rules.py`), declared once and never
  restated in `clean()` or a service. The container migrates on boot, so a new
  one on a table that holds rows first passes `manage.py constraint_preflight
  --path <database>` against production data, in a migration operation of its
  own.
- A JSON column read through a declared shape (a pydantic model, a kind's
  spec) is a `StoredJSONField` whose `load` is the one function its readers
  call (`hq/platform/core/stored_json.py`). The release rehearsal loads every
  stored value through it, so a shape that refuses what production holds
  ships with the migration that rewrites those rows.
- Every mutation is atomic and audit-attributed. Machine writes are safely
  retryable with durable idempotency.
- Money computed in the database (a `GeneratedField`, an annotation) works
  in whole cents, exact integers, and rounds there, as
  `business_use.deductible` does: SQLite holds a decimal as a double, where a
  half cent is inexact. A test holds it to `quantize_money` over every cent.
- A change to `hq_sdk`'s shape reaches every extension: a break fails
  `lint:sdk-breaking` until `PLUGIN_API_VERSION` moves with it, and mypy over
  the extensions holds what each export accepts and returns. A use an
  extension must not be able to write gets a case in
  `tests/sdk_consumer/consumer.py` ([Plugins](docs/Plugins.md)).
- The shared mechanisms (a list view's table, a command's link, the derived
  files, plugin imports) are held by the architecture tests, the
  `lint:drift-*` gates and `hq.platform.application.plugin_validation`, whose
  failures name the one way to do it.
- An href built from data HQ did not write (a reading, an attestation, a
  manifest, the public contact form) passes through `web_url`, in Python or as
  the template filter. It is then an http(s) address or no link at all.
- A dependency used by a plugin must survive a clean wheel install with
  `--no-deps`; the host-owned plugin check reproduces that production boundary.
- A workflow's write to GitHub (a deployment status, a push, a pull request)
  uses a token minted from HQ's GitHub App for that one permission, declared
  in `deploy/github-apps.json`; the job's own permissions stay at read.

## Frontend quality bar

A change to a shared part reads and edits its `Part` declaration, whose
`rules` [Shared UI](docs/Shared-UI.md#each-parts-rules) states under its name
and whose `example` and `states` the component catalog draws;
[Design](docs/Design.md) holds what holds across parts. Grow a primitive rather
than styling a page.

- Server-render useful HTML first; JavaScript progressively enhances working
  links and forms.
- The stylesheet is held to the best current practice: layout is the
  browser's, computed by CSS (container queries, `:has()`, intrinsic sizing,
  cascade layers, logical properties), never measured and reshaped by a
  script. Before writing a layout mechanism, check how mature design systems
  and the platform already solve it.
- Prefer shared primitives over page-specific markup or scripts. Do not add a
  framework or dependency for behavior the platform already provides.
- Keep interactions immediate, keyboard accessible, responsive, and stable
  under partial replacement. Preserve focus and browser history intentionally.
- A part of a page that is fetched, refreshed or polled is a `data-fragment`
  region answered from a `{% partialdef %}`. No script parses a response or
  keeps a timer of its own.
- A GET has no effect: pages are prefetched on press, and a speculative request
  is refused wherever HQ records or reaches out
  (`hq/platform/core/speculation.py`).
- Avoid N+1 queries. Prefetch relation panels and add a query-budget regression
  test for a projection that can grow with data or plugins.
- Scripts are deferred and respect `prefers-reduced-motion`; only the one
  `file_rules.BEFORE_DRAWING` names runs before the first frame. Nothing moves
  once a page is drawn: a script fills space the server's HTML already holds.

## Structural checks

- A Ruff rule is switched off only in `pyproject.toml`, with the reason beside
  it; a line-level `# noqa` names its code and says why.
- A `# type: ignore[code]` is for a wrong stub, with the reason.
- A new `lint:ast-grep` rule is a file of its own, with its reason, in
  `tools/lint/ast-grep/rules`, with its scope in its own `files:` and
  `ignores:` (`sgconfig.yml` there names the scopes), and its cases in
  `rule-tests/`.
- A piece only one home may use lives in a module of its own, which a
  `protected` contract in `pyproject.toml` lets only that home import
  (`lint:imports`), never an ast-grep rule naming it. The home imports it
  whole (`from . import idempotency`), so it re-exports none of its names.
- Python says a thing the one way HQ already has for it, never by hand:
  - a text cut to a length with an ellipsis is `Truncator(text).chars(n)`;
  - a raised first letter is `capfirst(text)`;
  - a list in words is `get_text_list(items, "and")`;
  - a value that must hold something is `RequiredText` or
    `Annotated[<type>, NonEmpty()]` from `command_fields`, never
    `min_length=1`;
  - an ISO instant or day is read by `moments.moment` or `stamp_day`.
- A docstring says what the thing is and why it exists. How it works lives in
  the one doc or comment that owns the mechanism, so a long docstring is
  trimmed as its file is touched.
- Duplication and tangling are green in every test; they are graph questions.
  The structural bar (`lint:structural`) fails a new near-duplicate pair
  outside tests, and its baseline never grows to pass. The other questions
  and the graph engine's traps: [Structural Gate](docs/Structural-Gate.md).
  Index the checkout you work in before asking the graph anything.
- An extension's use of `hq_sdk` is an import from another repository, so the
  graph has no edge for it: a change to `hq_sdk.capabilities`, `hq_sdk.ui` or
  `hq_sdk.audit` reads as safe here. Grep the extension checkouts.

## Definition of done

A piece of work is done when `mise run fast` passes on its tip, with the
tests its change touches (`mise run test <labels>`). A change to what a page
shows also needs `mise run dev:look` on each page its templates draw.

The whole-tree runs belong to CI and `check`. A shared sheet or part reaches
pages the change never named, so the browser suite on the pushed branch judges
every page in both engines; no whole-tree run is repeated on the Mac.

That branch is ready when `mise run check --release` exits 0, or
`mise run check --release --remote` for a pushed commit: every CI gate, the
composed suite with the extension set required, and read-only checks of the
deploy host (`hq-host check`, [Deployment](docs/Deployment.md)). `--skip-host`
exits 2, never 0, and so does a host whose release nothing verified; the check
says how to verify it.

- Tests cover success, denial, invalid input, and the regression class where
  applicable, not only the happy path.
- A change to templates or CSS is walked in a real browser through the
  workflow it serves, and the branch it lands on passes CI's browser suite.
- Docs change when a supported contract changes.
- No private identifiers, generated artifacts, secrets, or unrelated edits
  enter the diff.
- A push or a deploy waits for an explicit ask.
