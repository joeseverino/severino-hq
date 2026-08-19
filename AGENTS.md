# HQ engineering contract

This file is the shortest path from a fresh checkout to a safe change. It is
authoritative for human and agentic development in this public repository.

## Start here

1. Run `git status --short`; preserve unrelated work.
2. Read the nearest code and tests before changing an interface.
3. Run `./scripts/check.sh` before handing work back.

`check.sh` runs the suite three ways: with `DEBUG` on, with it off as production
runs it, and — when an extension set is supplied — with every extension
installed. That third pass is the one that catches what CI cannot, because the
host and its extensions first meet during compose, long after the merge button.
Supply it an interpreter that has them importable, which this repository's own
venv deliberately does not:

```sh
CHECK_PYTHON=/path/to/venv/bin/python \
SEVERINO_HQ_PLUGINS=… PYTHONPATH=… ./scripts/check.sh
```

`hq dev` already computes both values. Without them the pass is skipped, so
public CI and a fresh checkout are unaffected.

Local development uses `./scripts/dev.sh`. It collects assets and runs the same
ASGI/Uvicorn path as production with reload enabled. `hq dev` remains a local
convenience when the Severino tools CLI is available.

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

## Public host, private domains

This repository is public. Private first-party plugins are separate packages.
Never commit their inventory, repository identifiers, routes, domain models,
fixtures, workflows, or business vocabulary here. Public examples and tests
must use the synthetic `example.*` namespace. Runtime-supplied composition
metadata is the only place the real installed set meets the host.

Generic integration policy belongs here. Domain meaning belongs in its private
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
- No inline scripts, event handlers, or styles; the CSP and architecture tests
  enforce the shared delivery model.

## Definition of done

- The requested behavior is implemented at the correct layer.
- Tests cover success, denial, invalid input, and the regression class where
  applicable—not only the happy path.
- `./scripts/check.sh` passes.
- Docs change when a supported contract changes.
- No private identifiers, generated artifacts, secrets, or unrelated edits
  enter the diff.
- Do not commit, push, deploy, or modify private repositories unless the user
  explicitly asks for that operation.
