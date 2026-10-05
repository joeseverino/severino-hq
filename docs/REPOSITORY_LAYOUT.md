# Repository layout

HQ is an unpackaged Django application managed by uv. The checkout is the
deployment unit; the Go controller has its own module and build.

```text
hq/
  config/                 settings, URLs and ASGI/WSGI entrypoints
  platform/
    application/          shared use cases, authorization and integration contracts
    core/                 platform persistence, rendering and authentication
    api/                  HTTP delivery adapter and derived OpenAPI description
    mcp/                  MCP delivery adapter
    search_index/         shared search persistence
  domains/                domain apps, models, declarations and migrations
hq_sdk/                   supported Python surface for extensions
tests/
  fixtures/               synthetic extension used by host tests
  fuzz/                   explicit property-test harnesses
controller/               independently built Go module
templates/                shared and domain templates
static/                   authored assets and pinned upstream bundles
scripts/                  development, verification and deployment programs
docs/                     supported contracts and operator guidance
var/                      ignored local runtime state
pyproject.toml            dependencies, groups and Python tool configuration
uv.lock                   committed resolved versions and artifact hashes
```

Tests and migrations remain beside the app they verify. Shared templates and
authored assets retain their existing roots. Domain identifiers, URL namespaces,
capability names and database app labels are independent of Python package paths.
For example, `hq.domains.projects` still owns the `projects` app label and its
existing tables. The HTTP adapter keeps its `hq_api` app label.

## Environments

`uv run --locked`, which every `mise run` task uses, creates the host
development environment from `uv.lock` with the default `dev` and `tools`
groups. Browser checks also need the `browser` group and the Playwright browser
installation; `mise run browser` supplies both. Runtime exports explicitly omit all default
groups and retain hashes; image installation verifies those hashes.

The host does not declare its installed extensions. An assembled environment
installs independently admitted extension wheels. The local composed suite
(`mise run suite:composed`) installs nothing: it uses the `HQ_LOCAL_PYTHONPATH`
and `HQ_LOCAL_PLUGINS` values a gitignored `mise.local.toml` supplies. Do not run
an exact host sync over that environment: it would remove packages outside the
host dependency graph. Use the supported composition and plugin-check programs.

## Runtime state

Local defaults live in `var/db`, `var/media`, `var/exports` and `var/static`.
Environment overrides retain the production volume paths. Package relocation
does not move deployed data, mounted volumes or an existing local database.

When a checkout has an existing database at the old local default, explicitly
select it with `SEVERINO_DATABASE_PATH` until moving it. Stop the application,
make a SQLite backup that includes committed WAL contents, and verify the new
database before selecting its path. Keep the source database until the restored
application and migrations have been checked. Collected static assets can be
regenerated with `manage.py collectstatic`; an image collects its own when it
is built.

## Import boundary

Extensions import `hq_sdk`. The `hq` namespace is private implementation code;
the SDK validator rejects it. Moving implementation modules does not add aliases
for old package paths. Historical migration dependencies and foreign-key model
labels keep their original identities, while serialized Python callables use
their new import locations.
