"""Plugin declaration contract."""

from application.plugins import (
    PLUGIN_API_VERSION,
    NavigationItem,
    PluginManifest,
    installed_plugins,
    plugin_attention_items,
    plugin_dashboard_sections,
    plugin_overviews,
    plugin_resource_specs,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "NavigationItem",
    "PluginManifest",
    "installed_plugins",
    "plugin_attention_items",
    "plugin_dashboard_sections",
    "plugin_overviews",
    "plugin_resource_specs",
]
