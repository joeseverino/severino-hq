"""Versioned, explicitly allowlisted extension contract for trusted HQ plugins."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
from importlib import import_module
import os
import re
from typing import Any, Callable, Iterable

from django.core.exceptions import ImproperlyConfigured
from django.urls import include, path, reverse

from .ui import STATUS_VALUES, DomainOverview

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
    # Empty renders inline in the primary nav; a name collects this item into
    # the matching dropdown. Defaults to inline so a surface stays reachable in
    # one click, and so existing manifests keep their current placement.
    group: str = ""


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
    overview_provider: str = ""
    # Optional. Returns Insight-shaped items this extension believes need an
    # operator decision now. Composing surfaces gather these across extensions,
    # so a thing that needs doing is visible without opening its own page.
    attention_provider: str = ""
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


# A card is a scalar and a link, plus optional interpretation. Kept deliberately
# small: the aggregate is rendered by surfaces that compose many extensions at
# once, so a card must be cheap to produce and safe to show beside any other.
CARD_REQUIRED_KEYS = frozenset({"id", "label", "value", "url"})
CARD_OPTIONAL_KEYS = frozenset({"detail", "status", "trend"})
# Imported, not restated: one status vocabulary serves every surface that shows
# state, so a card and an insight cannot drift apart on what "attention" means.
CARD_STATUS_VALUES = STATUS_VALUES
# Direction of travel, not a judgement: whether "up" is good is the domain's
# business, which is what `status` is for.
CARD_TREND_VALUES = frozenset({"up", "down", "flat"})


def plugin_dashboard_sections() -> tuple[dict[str, Any], ...]:
    """Dashboard cards grouped by the extension that produced them.

    Attribution is authoritative: the host already knows which extension a
    provider belongs to, so a surface composing several of them never has to
    infer ownership from an id-naming convention that nothing enforces.
    """
    sections = []
    for plugin in installed_plugins():
        if not plugin.dashboard_provider:
            continue
        cards = tuple(_import(plugin.dashboard_provider)())
        _validate_dashboard_cards(cards)
        sections.append(
            {
                "id": plugin.id,
                "label": plugin.name,
                "url": (
                    reverse(plugin.navigation[0].route)
                    if plugin.navigation
                    else cards[0]["url"] if cards else ""
                ),
                "cards": cards,
            }
        )
    return tuple(sections)


def plugin_dashboard_cards() -> tuple[dict[str, Any], ...]:
    # Derived from the grouped form so the flat and grouped views cannot
    # disagree about what was provided.
    return tuple(
        card for section in plugin_dashboard_sections() for card in section["cards"]
    )


def plugin_overviews() -> tuple[dict[str, Any], ...]:
    """Typed rich overviews with attribution owned by the host registry."""
    sections = []
    for plugin in installed_plugins():
        if not plugin.overview_provider:
            continue
        overview = _import(plugin.overview_provider)()
        if not isinstance(overview, DomainOverview):
            raise ImproperlyConfigured(
                f"Plugin {plugin.id!r} overview_provider must return DomainOverview."
            )
        sections.append({"id": plugin.id, "label": plugin.name, "overview": overview})
    return tuple(sections)


def _validate_dashboard_cards(cards: Iterable[dict[str, Any]]) -> None:
    for card in cards:
        if not isinstance(card, dict) or not CARD_REQUIRED_KEYS <= card.keys():
            raise ImproperlyConfigured(
                "Plugin dashboard cards require id, label, value, and url."
            )
        # Unknown keys are rejected rather than ignored: a typo'd optional key
        # would otherwise silently render nothing at all.
        unknown = card.keys() - CARD_REQUIRED_KEYS - CARD_OPTIONAL_KEYS
        if unknown:
            raise ImproperlyConfigured(
                f"Unknown dashboard card keys: {', '.join(sorted(unknown))}."
            )
        status = card.get("status")
        if status is not None and status not in CARD_STATUS_VALUES:
            raise ImproperlyConfigured(
                f"Dashboard card status must be one of "
                f"{', '.join(sorted(CARD_STATUS_VALUES))}; got {status!r}."
            )
        trend = card.get("trend")
        if trend is not None and trend not in CARD_TREND_VALUES:
            raise ImproperlyConfigured(
                f"Dashboard card trend must be one of "
                f"{', '.join(sorted(CARD_TREND_VALUES))}; got {trend!r}."
            )


# Most urgent first. An operator scanning this list top-down should hit the
# thing that will hurt soonest; "neutral" is context, not a call to action, and
# is excluded from attention entirely.
ATTENTION_ORDER = ("serious", "attention")


def plugin_attention_items() -> tuple[dict[str, Any], ...]:
    """What every installed extension believes needs a decision now.

    Each entry carries its source so a composing surface can say where the item
    came from without the extension having to restate its own name. Items are
    returned already ordered by severity: the ordering is a property of the
    status vocabulary, so every surface that renders them agrees on urgency
    rather than each re-sorting to its own taste.

    Takes no exclusions on purpose. An earlier signature let a composer drop a
    domain here and substitute that domain's `DomainOverview` -- which is a
    display surface, and truncates. The queue is the one place a `serious` item
    is guaranteed to appear, so nothing may quietly remove a domain from it.
    A composer that also renders per-domain panels should group these by
    `source_id` rather than fetch the same question from a second channel.
    """
    gathered: list[dict[str, Any]] = []
    for plugin in installed_plugins():
        if not plugin.attention_provider:
            continue
        for item in _import(plugin.attention_provider)():
            status = getattr(item, "status", None)
            if status not in STATUS_VALUES:
                raise ImproperlyConfigured(
                    f"{plugin.id!r} produced an attention item with status "
                    f"{status!r}; expected one of {', '.join(sorted(STATUS_VALUES))}."
                )
            if status not in ATTENTION_ORDER:
                continue
            gathered.append(
                {"item": item, "source": plugin.name, "source_id": plugin.id}
            )
    return tuple(
        sorted(
            gathered,
            key=lambda entry: (
                ATTENTION_ORDER.index(entry["item"].status),
                entry["source"],
                entry["item"].title,
            ),
        )
    )


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
