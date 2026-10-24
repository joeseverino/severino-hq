# HQ engineering contract

The shortest path from a fresh checkout to a safe change, for people and
agents alike. Setup, tasks and layout: [Development](docs/Development.md).

## Start here

1. Run `git status --short`; preserve unrelated work. `git fetch` and compare
   with your brief's base, or `origin/main` without one, before designing:
   work lands from more than one place, and the mechanism you need may already
   exist there.
2. Ready is the [definition of done](#definition-of-done).

`mise tasks` lists what to start from, `mise run <task> --help` what each
takes; `--hidden` adds the gates aggregates, CI and the operator run, each
still runnable by the name a verdict gives it. A gate closes on a verdict line
naming every failed task and test.

- `mise run fast [BASE]` while editing. It selects through Python imports and
  app names, so a rule held only by a string or a template needs `check`.
- `mise run check` before handing work back, when the machine can hold it (a
  worker runs `fast`, per its house rules); `mise run ci` is every gate CI
  runs.
- `mise run test <labels>`, and `mise run test --at <base> <labels>` to prove a
  new test fails without the fix, never a base worktree, snapshot or checkout
  made by hand.
- `mise run dev:scratch` in the agent's own worktree, never `mise run dev`.
- `mise run manage -- <command>` for Django, `fix` for what lint holds,
  `regen` for derived files.

A CI job runs the aggregate of its name, so a gate goes in its job's file under
`mise-tasks/` (`mise-tasks/checks.toml` for `checks:*`), never in a workflow.

The composed suite (`suite:as-image`, run as the image runs it) is where the
host and its extensions first meet, which CI cannot reach. The machine's own
gitignored `mise.local.toml` names the extension set, and an agent leaves it as
it is; without it the pass runs the host alone. `mise doctor project` says
whether the set is named, found and importable
([The extension set](docs/Development.md#the-extension-set)).

A test that reads the repository rather than running the product carries
`@tag("source")` and runs only in `checks:source` (`mise run checks:source
<labels>` for some), since no image ships those files. The suite runs in
parallel on WAL SQLite through `hq/platform/core/test_runner.py`: read it
before changing the test database. A new host domain:
[New Domain](docs/New-Domain.md).
The project's agent and skills, and a pass: [AI](docs/AI.md).

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
  authorized, schema validated, transactionally audited, and idempotent.

Detail: [Architecture](docs/Architecture.md), [Plugins](docs/Plugins.md),
[API](docs/API.md).

## The host does not know its extensions

HQ is a host. Its extensions are separate packages with their own
repositories, tests and release cycles, installed at composition time. This
repository names none of them: not their inventory, repository
identifiers, routes, models, fixtures or vocabulary. A host that names an
extension has taken a dependency on it, and the properties this design exists
for (add an extension without touching the host, run the host with none
installed, develop the two on independent schedules) quietly stop being true.
Examples and tests use the synthetic `example.*` namespace.

Runtime-supplied composition metadata is the only place the real installed
set meets the host; two tests in `hq/platform/application/tests/test_plugins.py`
keep it that way, and one that fails has found a coupling.

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
| Plugin identity or domain semantics | private plugin repository |
| Cross-plugin compatibility | generic composition check in HQ |

A provider is named once, in `ADMITTED`
(`hq/domains/control_plane/provider_adapters/__init__.py`); everything else
derives from its adapter's `DEFINITIONS` and `CONNECTIONS` and its readings
module, and `test_admission.py` there holds that to be enough. Its Go half in
`controller/providers/` is handed its connection as a typed value
(`r.Supplied.For`), never a setting by name ([Providers](docs/Architecture.md#providers)).

Tests live in each package's `tests/` directory. When a definition moves,
retarget every `mock.patch` that named its old home, since a patch of a name
no longer looked up there patches nothing. A check that walks a path (an
architecture test, a glob) fails when its path matches nothing.

## Rules that eliminate bug classes

- A new mechanism states its security properties in its design. What a
  resource can do is derived from topology facts, never from a per-kind flag.
- Never interpolate a newline-separated file list into `eval` or `bash -c`,
  where each line runs as a command: split it with `set --` and pass `"$@"`.
- Reject unknown input; Pydantic plugin commands inherit `StrictCommand`.
- Enforce authorization in the shared capability/view layer, not ad hoc in a
  template or handler.
- A rule one row must keep on its own is a `Rule` in the model's
  `Meta.constraints` (`hq/platform/core/rules.py`), declared once and never
  restated in `clean()` or a service. The container migrates on boot, so a new
  one on a table that holds rows first passes `manage.py constraint_preflight
  --path <database>` against production data, in a migration operation of its
  own.
- Every mutation is atomic and audit-attributed. Machine writes are safely
  retryable with durable idempotency.
- Plugin code imports `hq_sdk`, never `application`, `core`, or another host
  app (`python -m hq_sdk.validation src`).
- A change to `hq_sdk`'s shape (`hq_sdk/contract.json`) is a fleet change:
  decide whether `PLUGIN_API_VERSION` moves, and give a new export a case in
  `tests/sdk_consumer/consumer.py` ([Plugins](docs/Plugins.md)).
- Derived files are never edited by hand: `mise run regen` writes them and
  `mise run drift` names any that is behind.
- List views use `TableListMixin`; direct view mutations and MCP model access
  are rejected by architecture tests.
- A link to a command's form is built by
  `hq.platform.application.action_links.command_url` and nothing else
  (`test_remedy_links.py` follows every remedy HQ emits).
- An href built from data HQ did not write (a reading, an attestation, a
  manifest, the public contact form) passes through `web_url`, in Python or as
  the template filter, so it is an http(s) address or no link at all.
- A dependency used by a plugin must survive a clean wheel install with
  `--no-deps`; the host-owned plugin check reproduces that production boundary.
- A workflow's write to GitHub (a deployment status, a push, a pull request)
  uses a token minted from HQ's GitHub App for that one permission, declared
  in `deploy/github-apps.json`; the job's own permissions stay at read.

## Frontend quality bar

Read [Design](docs/Design.md), the primitives a page is built from, before
adding UI; grow a primitive rather than styling a page.

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
- Scripts are deferred and respect `prefers-reduced-motion`; only the two
  `test_script_loading.py` names run before the first frame. Nothing moves
  once a page is drawn: a script fills space the server's HTML already holds.

## Structural checks

- Ruff's C901, `CognitiveComplexityTests` and `FileSizeTests` have no
  allowance list: a function over either complexity bound is split into named
  steps, a file over the size bound by responsibility.
- A Ruff rule is switched off only in `pyproject.toml`, with the reason beside
  it; a line-level `# noqa` names its code and says why.
- `checks:mypy` holds the seams `pyproject.toml` names (`files`) fully
  annotated, and the browser test package to attributes and calls that exist,
  without demanding annotations there. A `# type: ignore[code]` is for a wrong
  stub, with the reason.
- A docstring says what the thing is and why it exists. How it works lives in
  the one doc or comment that owns the mechanism, so a long docstring is
  trimmed as its file is touched.
- Duplication and tangling are green in every test; they are graph questions.
  The structural bar (`checks:structural`) fails a new near-duplicate pair
  outside tests, and its baseline never grows to pass. The other questions
  and the graph engine's traps: [Structural Gate](docs/Structural-Gate.md).
  Index the checkout you work in before asking it anything.
- An extension's use of `hq_sdk` is an import from another repository, so the
  graph has no edge for it: a change to `hq_sdk.capabilities`, `hq_sdk.ui` or
  `hq_sdk.audit` reads as safe here. Grep the extension checkouts.

## Definition of done

"Ready" means `mise run preflight` exits 0, or `mise run preflight --remote`
for a pushed commit: every CI gate, the composed suite with the extension set
required, and read-only checks of the deploy host (`hq-host check`,
[Deployment](docs/Deployment.md)). `--skip-host` exits 2, never 0, and so does
a host whose release nothing verified; the check says how to verify it.

- Tests cover success, denial, invalid input, and the regression class where
  applicable, not only the happy path.
- `mise run check` passes, including the composed pass when a change touches
  `hq_sdk`.
- A change to templates or CSS passes `mise run browser` and is walked in a
  real browser through the workflow it serves.
- Docs change when a supported contract changes.
- No private identifiers, generated artifacts, secrets, or unrelated edits
  enter the diff.
- Push, deploy and changes to private repositories wait for an explicit ask;
  committing follows the orchestrating session's rules.
