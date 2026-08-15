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
    django_apps=("example_notes",),
    url_prefix="notes/",
    urlconf="example_notes.urls",
    navigation=(NavigationItem("Notes", "notes:list", "notes"),),
    dashboard_provider="example_notes.projections:dashboard_cards",
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

## Native and machine clients

Server composition and remote synchronization are intentionally separate. A
native client should consume a versioned JSON API derived from the same command
and query services used by the web and MCP adapters. Resource identifiers,
idempotency keys, pagination cursors, change cursors, and scoped authorization
belong to that transport contract; they do not belong in templates or plugin
registration. This preserves one domain implementation while allowing web,
automation, and phone clients to evolve independently.
