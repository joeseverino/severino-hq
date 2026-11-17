"""For declaring an extension: its manifest, navigation, and the integration HQ calls for each surface."""

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
lazy from hq.platform.application.finding_model import Finding, opens
lazy from hq.platform.application.finding_steps import Do, Run

__all__ = [
    "PLUGIN_API_VERSION",
    "Do",
    "Finding",
    "NavigationGroup",
    "NavigationItem",
    "PluginIntegration",
    "PluginManifest",
    "Run",
    "installed_plugins",
    "opens",
    "plugin_attention_items",
    "plugin_dashboard_sections",
    "plugin_overviews",
]
