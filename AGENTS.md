# HQ engineering contract

This file is the shortest path from a fresh checkout to a safe change. It is
authoritative for human and agentic development in this public repository.

## Start here

1. Run `git status --short`; preserve unrelated work.
2. Read the nearest code and tests before changing an interface.
3. Run `./scripts/check.sh` before handing work back.
4. Run `./scripts/ci-local.sh` before pushing.
5. Run `./scripts/preflight.sh` before calling a change ready. On a small
   machine, push first and run `./scripts/preflight.sh --remote`: it reads
   every check GitHub ran on that commit instead of running `ci-local.sh`.

`check.sh` answers "do my changes work?". `ci-local.sh` answers "will the
pipeline accept them?": ruff and mypy at the pinned versions, the shell gates, the
Django deployment check, `pip-audit`, the image build, and the suite *inside*
that image, which is where composition runs it. It prints what it could not
run rather than implying full coverage. Point it at every interpreter that has
the requirements installed, because CI runs a 3.13/3.14 matrix and a
version-specific failure is otherwise found by pushing:

```sh
SEVERINO_CI_PYTHONS="/path/py312/bin/python .venv/bin/python" \
SEVERINO_HQ_PLUGINS=… ./scripts/ci-local.sh
```

`check.sh` runs the suite three ways: with `DEBUG` on, with it off as production
runs it, and (when an extension set is supplied) with every extension
installed. That third pass is the one that catches what CI cannot, because the
host and its extensions first meet during compose, long after the merge button.
It needs an interpreter that has them importable, which this repository's own
venv deliberately does not.

Put those values in a gitignored `.env.dev` once (copy
`scripts/dev.env.example`) and both `check.sh` and `ci-local.sh` pick them up,
so the full gate is `./scripts/check.sh` with no arguments. They can still be
passed explicitly, and an explicit value wins:

```sh
CHECK_PYTHON=/path/to/venv/bin/python \
SEVERINO_HQ_PLUGINS=… PYTHONPATH=… ./scripts/check.sh
```

Without them the composed pass is skipped, so public CI and a fresh checkout are
unaffected, but locally that means the gate quietly covers less, which is the
reason the file exists.

Local development uses `./scripts/dev.sh`. It collects assets and runs the same
ASGI/Uvicorn path as production with reload enabled.

`check.sh` runs the suite in parallel, which roughly halves the gate's time.
`core/test_runner.py` is what makes that safe on WAL SQLite: read
it before changing anything about the test database. `CHECK_PARALLEL=1` rules
parallelism out when a failure looks order- or isolation-dependent.

Diagnosing a parallel failure needs `tblib` installed, or the real error is
replaced by `cannot pickle 'traceback' object`. `--parallel=1` also works.

## Architecture in one minute

- `application/` owns use cases, authorization, transactions, projections,
  capability execution, and plugin internals.
- Django apps own persistence and domain-specific models. Adapters may query
  for rendering; they do not mutate models directly.
- Web, CLI, MCP, and HTTP API are delivery adapters over the same application
  behavior. Never reimplement a business rule in an adapter.
- `hq_sdk/` is the only supported Python import surface for plugins.
- `templates/partials/`, `application/ui.py`, `application/tables.py`, and the
  matching `hq_sdk.*` modules are the shared frontend contract.
- The machine API's current contract is `/api/v2/`. Writes are capability
  authorized, schema validated, transactionally audited, and idempotent.

The detailed boundaries live in `docs/APPLICATION_ARCHITECTURE.md`,
`docs/PLUGINS.md`, and `docs/API.md`.

## The host does not know its extensions

HQ is a host. The extensions it runs are separate packages with their own
repositories, tests and release cycles, and they are installed at composition
time rather than vendored here: the same separation any platform keeps from the
things built on it.

So this repository names none of them: not their inventory, repository
identifiers, routes, models, fixtures or vocabulary. That is an architectural
constraint before it is anything else. A host that names an extension has taken
a dependency on it, and the properties this design exists for (add an extension
without touching the host, run the host with none installed, develop the two on
independent schedules) all quietly stop being true. Examples and tests use the
synthetic `example.*` namespace so the host can demonstrate a contract without
acquiring a consumer.

Runtime-supplied composition metadata is the only place the real installed set
meets the host, and two tests keep it that way (`application/tests/test_plugins.py`).
When one of them fails it has found a coupling, not a secret.

Generic integration policy belongs here. Domain meaning belongs in its own
package. If an abstraction has only one domain-specific caller, leave it in the
domain until a genuine shared contract appears.

## Where changes belong

| Change | Owner |
| --- | --- |
| Business rule or mutation | application service in its domain |
| HTTP, CLI, MCP, or view parsing/rendering | adapter calling that service |
| Plugin-facing primitive | implementation in HQ plus export from `hq_sdk` |
| Repeated layout or interaction | shared partial/CSS/JS primitive |
| Plugin identity or domain semantics | private plugin repository |
| Cross-plugin compatibility | generic composition check in HQ |

### Adding a provider

A provider emits itself: nothing outside its own modules names it except one
entry in `ADMITTED` (`control_plane/provider_adapters/__init__.py`). Its adapter
module declares its kinds (`DEFINITIONS`) and its `CONNECTIONS`; its readings
are a module in `control_plane/observations/`, found by discovery. Registries,
connection labels, credential policy, the controller's registry and the
topology edges its readings declare are all derived from those, and admission
fails at import on a duplicate or undeclared name. The controller half (its
readers, actions and probe) is Go, in `controller/providers/`, registered in
`providers.New`; the contract's `SweptKind` names every kind it reads.
`control_plane/provider_adapters/tests/test_admission.py` shows the contract;
`docs/APPLICATION_ARCHITECTURE.md` has the detail.

### Moving code

Tests live in each package's `tests/` directory. When a definition moves,
retarget every `mock.patch("module.name")` that named its old home: a patch of
a name that is no longer looked up there passes silently and patches nothing.
A check that walks a path (an architecture test, a script's glob) needs the
same review, and should fail when its path matches nothing rather than pass.

## Rules that eliminate bug classes

- Reject unknown input; Pydantic plugin commands inherit `StrictCommand`.
- Enforce authorization in the shared capability/view layer, not ad hoc in a
  template or handler.
- Every mutation is atomic and audit-attributed. Machine writes are safely
  retryable with durable idempotency.
- Plugin IDs, routes, Django apps, distributions, providers, grants, and
  capability names must fail at startup when invalid or conflicting.
- Plugin code imports `hq_sdk`, never `application`, `core`, or another host
  app. `python -m hq_sdk.validation src` enforces this.
- The shape of `hq_sdk` is committed in `hq_sdk/contract.json` and a test holds
  the exports to it. A change there is a fleet change: regenerate with
  `manage.py sdk_contract`, review the diff, and decide whether
  `PLUGIN_API_VERSION` moves.
- List views use `TableListMixin`; direct view mutations and MCP model access
  are rejected by architecture tests.
- A link to a command's form is built by `application.action_links.command_url`
  and nothing else, so its target is encoded the way the form reads it back.
  `application/tests/test_remedy_links.py` follows every remedy the findings, the
  action queue and the pages emit, and fails on one that opens with no target
  chosen or on the "replaces the whole record" form.
- An href built from data HQ did not write (a reading, an attestation, a
  manifest, the public contact form) passes through `web_url`, in Python or as
  the template filter, so it is an http(s) address or no link at all.
- Public tests compose synthetic siblings. The assembled private image runs
  all real plugin suites together.
- A dependency used by a plugin must survive a clean wheel install with
  `--no-deps`; the host-owned plugin check reproduces that production boundary.

## Frontend quality bar

`docs/DESIGN.md` is the design language: the primitives a page is built from
and the rules they keep. Read it before adding UI; grow a primitive rather
than styling a page.

- Server-render useful HTML first; JavaScript progressively enhances working
  links and forms.
- Prefer shared primitives over page-specific markup or scripts. Do not add a
  framework or dependency for behavior the platform already provides.
- Keep interactions immediate, keyboard accessible, responsive, and stable
  under partial replacement. Preserve focus and browser history intentionally.
- Avoid N+1 queries. Prefetch relation panels and add a query-budget regression
  test for a projection that can grow with data or plugins.
- Scripts are deferred; shared assets are content-versioned, compressed, and
  cached. Respect `prefers-reduced-motion`.
- No inline scripts or event handlers; the CSP and architecture tests enforce
  the shared delivery model. Styles are the one exception: `style-src` allows
  `'unsafe-inline'` so a chart can position a mark with a per-datum custom
  property (`style="--at: 62%"`), which no class expresses and no nonce covers.
  Use it for that and nothing else: a test pins `style-src` as the only
  relaxed directive, so a second one fails the suite rather than the review.

## Structural checks

Function complexity is part of the gate. Ruff's C901 fails any function whose
cyclomatic complexity exceeds 15, and `CognitiveComplexityTests` in
`application/tests/test_architecture.py` fails any non-test function whose cognitive
complexity exceeds 20. There is no allowance list: a function over either
limit is split into named steps.

The architectural seams are type checked. `python -m mypy` (a gate in
`ci-local.sh` and CI's Checks job) runs mypy with django-stubs over the modules
`mypy.ini` names: the security, capability, plugin, resource and
integration-spec contracts, the provider vocabulary and registry with the
provider modules split out of it, the controller's handler registry and
connection plumbing, `hq_sdk`, `hq_api` and `hq_mcp`, with every function in
them fully annotated. It loads `config/settings_typecheck.py`, the
host's settings without extensions. Fix what it reports rather than silencing
it; a `# type: ignore[code]` is for a stub that is wrong, with the reason beside
it. To widen it, add a module to `files` and to the strict section in
`mypy.ini` and fix what it finds.

Tests answer "does this behave?". They do not answer "is this still one system?":
duplication and tangling are green all the way down. Those are graph questions,
so ask a graph. With the repository indexed in a code knowledge graph, ask:

| Question | Query | Bar |
| --- | --- | --- |
| Did I re-implement something? | `MATCH (a)-[r:SIMILAR_TO]->(b) RETURN a.file_path, b.file_path, a.name, b.name` | no new pair outside tests |
| Hidden O(n²)? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.linear_scan_in_loop >= 1 RETURN f.qualified_name, f.linear_scan_in_loop` | every hit bounded by a fixed or small input |
| Did I tangle the call graph? | `get_architecture(aspects: ["cycles"])` | no new confirmed cycle |
| Is one file becoming the system? | `git ls-files '*.py' \| grep -v test \| xargs wc -l \| sort -n \| tail -4` | the largest files do not grow; split by provider before adding one |

Write the queries exactly as given, and mind two traps in this engine.
`is_test` is **not** reliable: test classes carry `is_test: false`, so a filter
on it silently counts the whole suite. And `NOT <prop> CONTAINS "..."` returns
**no rows at all** rather than the complement, so a negated filter reads as a
clean bar when it measured nothing. Filter positively, or return the rows and
exclude test paths by eye.

Re-index after a change and re-run them; a result that moved the wrong way is a
finding whether or not the suite is green.

`scripts/structural-bar.sh` runs the duplicate and largest-file checks as a
gate (`ci-local.sh` includes it) against `scripts/structural-baseline.txt`. Index
the checkout you work in before asking the graph anything; a worktree is its
own checkout.

Confirm a cycle before believing it. Python's `.get()` on a dict resolves to any
class method named `get`, so view classes turn up in cycles they have nothing to
do with: read the function and check it really calls into the loop. The same
caution applies to `trace_path`: its first hop is exact, deeper hops resolve
generic names (`get`, `handle`, `search`) optimistically. Use it for blast
radius, verify before acting on a three-hop claim.

One thing the graph cannot see: an extension's use of `hq_sdk` is an in-process
import from another repository, so no edge exists for it. Changing
`hq_sdk.capabilities`, `hq_sdk.ui` or `hq_sdk.audit` is a fleet-wide change that
this repository's graph will report as safe. Grep the extension checkouts.

## Definition of done

"Ready" means `./scripts/preflight.sh` exits 0. It runs `ci-local.sh` with
every gate required, `check.sh` with the composed pass required, and read-only
checks of the deploy host over SSH (`SEVERINO_HQ_DEPLOY_HOST`): the checkout's
ownership, the runner's sudo rule, the root-owned programs against this commit,
the installed units and free disk. `--skip-host` runs the local gates only and
exits 2, never 0.

- The requested behavior is implemented at the correct layer.
- Tests cover success, denial, invalid input, and the regression class where
  applicable, not only the happy path.
- `./scripts/check.sh` passes, including the composed pass when a change
  touches `hq_sdk`, because the host and its extensions first meet there.
- A change to templates or CSS passes the browser layout gate:
  `CHECK_BROWSER=1 ./scripts/check.sh` (Playwright, set up as the README's
  development section shows). A UI change is also walked in a real browser
  through the workflow it serves, not checked page by page.
- `./scripts/ci-local.sh` passes before a push, including its code scanning
  gates (no CodeQL alert and every file-based Scorecard check at 10) and the
  browser layout gate, which it always runs.
- A browser check selects markup only through `SELECTORS` in
  `core/browser_tests.py`; `core/tests/test_browser_selectors.py` holds every one
  to a template that renders it.
- The structural bar above did not move the wrong way.
- Docs change when a supported contract changes.
- No private identifiers, generated artifacts, secrets, or unrelated edits
  enter the diff.
- Do not commit, push, deploy, or modify private repositories unless the user
  explicitly asks for that operation.
