"""The extension's one declaration, read by HQ and by the check alike."""

from hq_sdk.plugin import NavigationItem, PluginIntegration, PluginManifest


def ready() -> bool:
    return True


def integration() -> PluginIntegration:
    return PluginIntegration(health=ready)


plugin = PluginManifest(
    id="example.extension",
    name="Example extension",
    version="1.0.0",
    distribution="example-extension",
    source_repository="example/example-extension",
    source_workflow=".github/workflows/admit-plugin.yml",
    api_version=8,
    integration_provider="example_extension.plugin:integration",
    django_apps=("example_extension",),
    url_prefix="examples/extension/",
    urlconf="example_extension.urls",
    navigation=(NavigationItem("Example", "example_extension:index", "example_extension"),),
)
