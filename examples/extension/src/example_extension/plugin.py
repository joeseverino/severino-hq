"""The extension's one declaration, read by HQ and by the check alike."""

from hq_sdk.plugin import PLUGIN_API_VERSION, NavigationItem, PluginIntegration, PluginManifest


def ready() -> bool:
    return True


def integration() -> PluginIntegration:
    from . import demo

    return PluginIntegration(health=ready, demo_seed=demo.plant, demo_pages=demo.pages)


plugin = PluginManifest(
    id="example.extension",
    name="Example extension",
    version="1.0.0",
    distribution="example-extension",
    source_repository="example/example-extension",
    source_workflow=".github/workflows/admit-plugin.yml",
    # The host's own constant, where an extension writes the number: this one
    # ships in the host's tree, so it is never a wheel built for another host.
    api_version=PLUGIN_API_VERSION,
    integration_provider="example_extension.plugin:integration",
    django_apps=("example_extension",),
    url_prefix="examples/extension/",
    urlconf="example_extension.urls",
    navigation=(NavigationItem("Example", "example_extension:index", "example_extension"),),
    description="One page that shows an extension composed into HQ.",
)
