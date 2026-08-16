# Plugin Architecture

Severino HQ exposes a versioned extension contract for trusted, installable
Django packages. A plugin declares its integration once; HQ derives application
installation, routing, navigation, dashboard projections, global search,
readiness, authorization, and capability-adapter exposure from that manifest.

Plugins are disabled by default. Deployment explicitly allowlists each trusted
entry point with `SEVERINO_HQ_PLUGINS`, using comma-separated
`package.module:attribute` references. Importable code is never discovered and
executed automatically.

```python
from application.plugins import NavigationItem, PluginManifest

plugin = PluginManifest(
    id="example.notes",
    name="Notes",
    version="1.0.0",
    distribution="example-notes",
    source_repository="example/example-notes",
    source_workflow=".github/workflows/admit-plugin.yml",
    django_apps=("example_notes",),
    url_prefix="notes/",
    urlconf="example_notes.urls",
    navigation=(NavigationItem("Notes", "notes:list", "notes"),),
    dashboard_provider="example_notes.projections:dashboard_cards",
    overview_provider="example_notes.projections:domain_overview",
    capability_provider="example_notes.capabilities:specs",
    search_provider="example_notes.projections:search_definitions",
    health_provider="example_notes.health:ready",
    operator_capabilities=("notes.read", "notes.write"),
    mcp_read_capabilities=("notes.read",),
    mcp_write_capabilities=("notes.write",),
)
```

Set `SEVERINO_HQ_PLUGINS=example_notes.plugin:plugin`, install the package in
the deployment image, and run its migrations. `python manage.py plugins` emits
the effective, machine-readable inventory and validates compatibility.

## Cordon admission

Production loading is fail-closed. When plugins are enabled with `DJANGO_DEBUG`
off, HQ requires `SEVERINO_HQ_PLUGIN_LOCK` and
`SEVERINO_HQ_PLUGIN_POLICY_SHA256`. The lock inventory must exactly equal the
enabled manifest inventory. Every entry binds the plugin ID, distribution,
installed version, host API version, immutable source commit, wheel SHA-256,
and Cordon policy SHA-256.

The canonical policy is `policy/plugin-admission-v1.json`. Admission requires
the plugin contract and package tests, a dependency lock and audit, secret
scan, wheel SBOM, and wheel vulnerability scan with no fixable high or critical
findings. Private composition CI signs the statement with GitHub OIDC through
Sigstore. Cordon verifies the signature bundle against the exact repository and
workflow identity before installation, then emits the lock entry embedded in
the composed image.

The lock is evidence of a specific artifact under a specific policy. It is not
a claim that arbitrary plugin code or future versions are safe. A changed
wheel, version, workflow, policy, host API, or enabled inventory requires a new
approval and image build.

## Composition

Production runs **one image carrying every admitted plugin**. Plugins do not
build or deploy images: each verifies and admits itself, publishes its signed
bundle, and triggers the host's composition workflow. When a plugin shipped its
own image, deploying one replaced the others and silently dropped them.

The composition workflow is the only path to production. It verifies each
signature itself, against the identity built from the declared repository and
workflow, so a plugin cannot widen who may sign for it by editing its own
repository. Entries are merged into one lock by Cordon's lock tool, which
already accepts several entries — the host does not reimplement it.
`SEVERINO_HQ_PLUGINS` is derived from the merged lock, because the enabled and
approved inventories must be identical or the host refuses to start.

The declared set lives in a repository variable rather than a committed file:
this repository is public and the extensions it composes are not.
`composition/extensions.json` documents the shape.

## Contract boundary

The plugin API is for trusted code that ships with an HQ deployment. External
or untrusted systems integrate through authenticated HTTP or MCP adapters, not
through runtime code loading. Providers contain projection logic only; domain
rules remain in the plugin's application services so web pages, commands,
search, MCP, and future native clients cannot develop conflicting behavior.

`api_version` fails closed on incompatibility. Additive manifest fields remain
compatible within a version; removals or semantic changes require the next API
version. Plugin identifiers are stable, reverse-DNS-style names and must not be
reused.

## Shared UI contract

Installable modules inherit HQ's design system and should not ship a parallel
stylesheet for ordinary application structure. API v1 guarantees these host
templates:

| Template | Contract |
| --- | --- |
| `base.html` | Authenticated shell, navigation, messages, static assets, and security metadata |
| `partials/_page_head.html` | Page title, lede, and optional primary action |
| `partials/_kpi_grid.html` | Responsive linked or static KPI collection |
| `partials/_timeline.html` | Chronological linked events from `application.ui.Timeline` |
| `partials/_stacked_bar_chart.html` | Accessible chart from `application.ui.StackedBarChart` |
| `partials/_empty_state.html` | Consistent empty state and optional action |
| `partials/_form_field.html` | Label, control, help text, and validation errors |
| `partials/_pagination.html` | Query-preserving paginated navigation |

Standard cards, section headings, data tables, list rows, forms, buttons, tags,
and two-column layouts use the classes demonstrated by `example_hq_plugin`.
Plugin templates supply domain content while HQ owns layout behavior, tokens,
responsive rules, accessibility states, and visual evolution. A new shared
pattern belongs in HQ first; copying host CSS or markup into every plugin is a
contract failure.

## Native and machine clients

Server composition and remote synchronization are intentionally separate. A
native client should consume a versioned JSON API derived from the same command
and query services used by the web and MCP adapters. Resource identifiers,
idempotency keys, pagination cursors, change cursors, and scoped authorization
belong to that transport contract; they do not belong in templates or plugin
registration. This preserves one domain implementation while allowing web,
automation, and phone clients to evolve independently.
