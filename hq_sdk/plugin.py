"""Plugin declaration contract."""

from hq.platform.application.plugins import (
    PLUGIN_API_VERSION,
    NavigationGroup,
    NavigationItem,
    PluginIntegration,
    PluginManifest,
    installed_plugins,
    plugin_attention_items,
    plugin_dashboard_sections,
    plugin_overviews,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "NavigationGroup",
    "NavigationItem",
    "PluginIntegration",
    "PluginManifest",
    "installed_plugins",
    "plugin_attention_items",
    "plugin_dashboard_sections",
    "plugin_overviews",
]
