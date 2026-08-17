"""Plugin declaration contract."""

from application.plugins import (
    PLUGIN_API_VERSION,
    NavigationItem,
    PluginManifest,
    installed_plugins,
    plugin_attention_items,
    plugin_dashboard_sections,
    plugin_overviews,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "NavigationItem",
    "PluginManifest",
    "installed_plugins",
    "plugin_attention_items",
    "plugin_dashboard_sections",
    "plugin_overviews",
]
