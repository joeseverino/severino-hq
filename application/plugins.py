"""Versioned, explicitly allowlisted extension contract for trusted HQ plugins."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
from importlib import import_module
import os
import re
from typing import Any, Callable, Iterable

from django.core.exceptions import ImproperlyConfigured
from django.urls import include, path

PLUGIN_API_VERSION = 1
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
PLUGIN_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLUGIN_WORKFLOW = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")


@dataclass(frozen=True)
class NavigationItem:
    label: str
    route: str
    namespace: str
    order: int = 500


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    distribution: str
    source_repository: str
    source_workflow: str
    api_version: int = PLUGIN_API_VERSION
    django_apps: tuple[str, ...] = ()
    url_prefix: str = ""
    urlconf: str = ""
    navigation: tuple[NavigationItem, ...] = ()
    dashboard_provider: str = ""
    capability_provider: str = ""
    search_provider: str = ""
    health_provider: str = ""
    operator_capabilities: tuple[str, ...] = ()
    mcp_read_capabilities: tuple[str, ...] = ()
    mcp_write_capabilities: tuple[str, ...] = ()


def _import(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ImproperlyConfigured(
            f"Plugin reference {spec!r} must use 'module:attribute'."
        )
    try:
        return getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise ImproperlyConfigured(f"Cannot load HQ plugin reference {spec!r}.") from exc


def _references() -> tuple[str, ...]:
    raw = os.environ.get("SEVERINO_HQ_PLUGINS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _validate(manifest: PluginManifest, reference: str) -> None:
    if not isinstance(manifest, PluginManifest):
        raise ImproperlyConfigured(f"{reference!r} did not expose PluginManifest.")
    if not PLUGIN_ID.fullmatch(manifest.id):
        raise ImproperlyConfigured(f"Invalid HQ plugin id {manifest.id!r}.")
    if not PLUGIN_DISTRIBUTION.fullmatch(manifest.distribution):
        raise ImproperlyConfigured(
            f"Invalid HQ plugin distribution {manifest.distribution!r}."
        )
    if not PLUGIN_REPOSITORY.fullmatch(manifest.source_repository):
        raise ImproperlyConfigured(
            f"Invalid HQ plugin repository {manifest.source_repository!r}."
        )
    if not PLUGIN_WORKFLOW.fullmatch(manifest.source_workflow):
        raise ImproperlyConfigured(
            f"Invalid HQ plugin workflow {manifest.source_workflow!r}."
        )
    if manifest.api_version != PLUGIN_API_VERSION:
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} requires API {manifest.api_version}; "
            f"HQ supports {PLUGIN_API_VERSION}."
        )
    if bool(manifest.url_prefix) != bool(manifest.urlconf):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} must declare url_prefix and urlconf together."
        )
    for item in manifest.navigation:
        if not item.namespace or not item.route:
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} has an incomplete navigation item."
            )


@cache
def installed_plugins() -> tuple[PluginManifest, ...]:
    plugins = []
    ids = set()
    for reference in _references():
        manifest = _import(reference)
        _validate(manifest, reference)
        if manifest.id in ids:
            raise ImproperlyConfigured(f"Duplicate HQ plugin id {manifest.id!r}.")
        ids.add(manifest.id)
        plugins.append(manifest)
    installed = tuple(sorted(plugins, key=lambda item: item.id))
    from .plugin_admission import enforce_plugin_admission

    enforce_plugin_admission(installed)
    return installed


def installed_plugin_apps() -> list[str]:
    return [app for plugin in installed_plugins() for app in plugin.django_apps]


def plugin_urlpatterns() -> list:
    return [
        path(plugin.url_prefix, include(plugin.urlconf))
        for plugin in installed_plugins()
        if plugin.urlconf
    ]


def plugin_navigation() -> tuple[NavigationItem, ...]:
    return tuple(
        sorted(
            (item for plugin in installed_plugins() for item in plugin.navigation),
            key=lambda item: (item.order, item.label),
        )
    )


def _provided(attribute: str) -> tuple[Any, ...]:
    values = []
    for plugin in installed_plugins():
        reference = getattr(plugin, attribute)
        if reference:
            provider: Callable[[], Iterable[Any]] = _import(reference)
            values.extend(provider())
    return tuple(values)


def plugin_dashboard_cards() -> tuple[dict[str, Any], ...]:
    cards = _provided("dashboard_provider")
    required = {"id", "label", "value", "url"}
    for card in cards:
        if not isinstance(card, dict) or not required <= card.keys():
            raise ImproperlyConfigured(
                "Plugin dashboard cards require id, label, value, and url."
            )
    return cards


def plugin_capability_specs() -> tuple[Any, ...]:
    return _provided("capability_provider")


def plugin_search_definitions() -> tuple[Any, ...]:
    return _provided("search_provider")


def plugin_health() -> dict[str, bool]:
    checks = {}
    for plugin in installed_plugins():
        checks[plugin.id] = (
            bool(_import(plugin.health_provider)()) if plugin.health_provider else True
        )
    return checks


def plugin_capabilities(kind: str) -> frozenset[str]:
    attribute = {
        "operator": "operator_capabilities",
        "mcp_read": "mcp_read_capabilities",
        "mcp_write": "mcp_write_capabilities",
    }[kind]
    return frozenset(
        capability
        for plugin in installed_plugins()
        for capability in getattr(plugin, attribute)
    )


def describe_plugins() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": PLUGIN_API_VERSION,
        "plugins": [
            {
                **asdict(plugin),
                "navigation": [asdict(item) for item in plugin.navigation],
            }
            for plugin in installed_plugins()
        ],
    }
