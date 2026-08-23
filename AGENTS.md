# HQ engineering contract

This file is the shortest path from a fresh checkout to a safe change. It is
authoritative for human and agentic development in this public repository.

## Start here

1. Run `git status --short`; preserve unrelated work.
2. Read the nearest code and tests before changing an interface.
3. Run `./scripts/check.sh` before handing work back.
4. Run `./scripts/ci-local.sh` before pushing.

`check.sh` answers "do my changes work?". `ci-local.sh` answers "will the
pipeline accept them?" — ruff at the pinned version, the shell gates, the
Django deployment check, `pip-audit`, the image build, and the suite *inside*
that image, which is where composition runs it. It prints what it could not
run rather than implying full coverage. Point it at every interpreter that has
the requirements installed, because CI runs a 3.12/3.13/3.14 matrix and a
version-specific failure is otherwise found by pushing:

```sh
SEVERINO_CI_PYTHONS="/path/py312/bin/python .venv/bin/python" \
SEVERINO_HQ_PLUGINS=… ./scripts/ci-local.sh
```

`check.sh` runs the suite three ways: with `DEBUG` on, with it off as production
runs it, and — when an extension set is supplied — with every extension
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
unaffected — but locally that means the gate quietly covers less, which is the
reason the file exists.

Local development uses `./scripts/dev.sh`. It collects assets and runs the same
ASGI/Uvicorn path as production with reload enabled. `hq dev` remains a local
convenience when the Severino tools CLI is available.

`check.sh` runs the suite in parallel, which is why the gate takes ~46s rather
than ~100s. `core/test_runner.py` is what makes that safe on WAL SQLite — read
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
time rather than vendored here — the same separation any platform keeps from the
things built on it.

So this repository names none of them: not their inventory, repository
identifiers, routes, models, fixtures or vocabulary. That is an architectural
constraint before it is anything else. A host that names an extension has taken
a dependency on it, and the properties this design exists for — add an extension
without touching the host, run the host with none installed, develop the two on
independent schedules — all quietly stop being true. Examples and tests use the
synthetic `example.*` namespace so the host can demonstrate a contract without
acquiring a consumer.

Runtime-supplied composition metadata is the only place the real installed set
meets the host, and two tests keep it that way (`application/test_plugins.py`).
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
- List views use `TableListMixin`; direct view mutations and MCP model access
  are rejected by architecture tests.
- Public tests compose synthetic siblings. The assembled private image runs
  all real plugin suites together.
- A dependency used by a plugin must survive a clean wheel install with
  `--no-deps`; the host-owned plugin check reproduces that production boundary.

## Frontend quality bar

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
  Use it for that and nothing else — a test pins `style-src` as the only
  relaxed directive, so a second one fails the suite rather than the review.

## Structural checks the test suite cannot make

Tests answer "does this behave?". They do not answer "is this still one system?"
— duplication, tangling and complexity creep are green all the way down. Those
are graph questions, so ask a graph. With the repository indexed in a code
knowledge graph, four queries carry the bar:

Write the query exactly as given. `is_test` is **not** reliable here — test
classes carry `is_test: false` — so every one of these excludes tests by
*path*. Filtering on `is_test` silently counts the test suite and yields a
number that looks like a regression and is not.

| Question | Query | Bar |
| --- | --- | --- |
| Did I re-implement something? | `MATCH (a)-[r:SIMILAR_TO]->(b) WHERE NOT a.file_path CONTAINS "test" AND NOT b.file_path CONTAINS "test" RETURN count(r)` | does not grow (currently 9) |
| Did a function get away from me? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.cognitive >= 22 AND NOT f.file_path CONTAINS "test" AND NOT f.file_path CONTAINS "migrations" RETURN count(f)` | no new entries (currently 7) |
| Hidden O(n²)? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.linear_scan_in_loop >= 1 AND NOT f.file_path CONTAINS "test" RETURN f.qualified_name` | 3, all pre-existing |
| Did I tangle the call graph? | `get_architecture(aspects: ["cycles"])` | 2, both confirmed false positives |

The two standing cycles resolve `.get()` on a dict to a class method named
`get`; read the function before believing a third. The three standing
`linear_scan_in_loop` hits are `plugins._validate_composition`,
`search._fallback_snippet` and `services._faults`.

Re-index after a change and re-run them; a number that moved the wrong way is a
finding whether or not the suite is green.

Confirm a cycle before believing it. Python's `.get()` on a dict resolves to any
class method named `get`, so view classes turn up in cycles they have nothing to
do with — read the function and check it really calls into the loop. The same
caution applies to `trace_path`: its first hop is exact, deeper hops resolve
generic names (`get`, `handle`, `search`) optimistically. Use it for blast
radius, verify before acting on a three-hop claim.

One thing the graph cannot see: an extension's use of `hq_sdk` is an in-process
import from another repository, so no edge exists for it. Changing
`hq_sdk.capabilities`, `hq_sdk.ui` or `hq_sdk.audit` is a fleet-wide change that
this repository's graph will report as safe. Grep the extension checkouts.

## Definition of done

- The requested behavior is implemented at the correct layer.
- Tests cover success, denial, invalid input, and the regression class where
  applicable—not only the happy path.
- `./scripts/check.sh` passes — including the composed pass when a change
  touches `hq_sdk`, because the host and its extensions first meet there.
- The structural bar above did not move the wrong way.
- Docs change when a supported contract changes.
- No private identifiers, generated artifacts, secrets, or unrelated edits
  enter the diff.
- Do not commit, push, deploy, or modify private repositories unless the user
  explicitly asks for that operation.
