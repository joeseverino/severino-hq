"""One declaration from which HQ derives every example integration surface."""

from hq_sdk.plugin import NavigationItem, PluginIntegration, PluginManifest


def integration() -> PluginIntegration:
    from .projections import dashboard_cards, ready

    return PluginIntegration(dashboard=dashboard_cards, health=ready)

plugin = PluginManifest(
    id="example.notes",
    name="Notes contract example",
    version="1.0.0",
    distribution="severino-hq",
    source_repository="joeseverino/severino-hq",
    source_workflow=".github/workflows/ci.yml",
    api_version=2,
    integration_provider="example_hq_plugin.plugin:integration",
    django_apps=("example_hq_plugin",),
    url_prefix="examples/notes/",
    urlconf="example_hq_plugin.urls",
    navigation=(NavigationItem("Example", "example_plugin:index", "example_plugin"),),
)
