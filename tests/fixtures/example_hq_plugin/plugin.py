"""One declaration from which HQ derives every example integration surface."""

from hq_sdk.plugin import NavigationItem, PluginIntegration, PluginManifest


def integration() -> PluginIntegration:
    from .outbound import work
    from .projections import dashboard_cards, ready

    return PluginIntegration(dashboard=dashboard_cards, health=ready, outbound=work)


plugin = PluginManifest(
    id="example.notes",
    name="Notes contract example",
    version="1.0.0",
    distribution="severino-hq",
    source_repository="joeseverino/severino-hq",
    source_workflow=".github/workflows/ci.yml",
    api_version=4,
    integration_provider="tests.fixtures.example_hq_plugin.plugin:integration",
    django_apps=("tests.fixtures.example_hq_plugin",),
    url_prefix="examples/notes/",
    urlconf="tests.fixtures.example_hq_plugin.urls",
    navigation=(NavigationItem("Example", "example_plugin:index", "example_plugin"),),
    operator_capabilities=("notes.read", "notes.write"),
)
