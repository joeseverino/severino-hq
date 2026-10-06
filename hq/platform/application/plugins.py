"""Versioned, explicitly allowlisted extension contract for trusted HQ plugins."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from functools import cache, lru_cache, partial
import hashlib
from importlib import import_module
import inspect
import itertools
import marshal
import os
import pickle
import re
import time
from types import CodeType
import weakref
from typing import Any, Callable, Iterable

from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, include, path
from hq.platform.application.routes import reverse

from .demo import showing_demo
from .derivations import derivation, forget
from .ui import STATUS_VALUES, DomainOverview

PLUGIN_API_VERSION = 4

# The manifest fields plugin API 1 named runtime providers with; API 2 has the
# single integration_provider entry point. A wheel that passes one fails in
# the manifest constructor, which names only the first unexpected keyword, so
# the loader recognises all of them to report the API epoch instead of an
# unexplained constructor error.
PLUGIN_API_1_PROVIDER_FIELDS = (
    "attention_provider",
    "capability_provider",
    "connection_provider",
    "dashboard_provider",
    "health_provider",
    "overview_provider",
    "resource_provider",
    "search_provider",
)
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
PLUGIN_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLUGIN_WORKFLOW = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
PYTHON_PATH = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
PYTHON_REFERENCE = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$"
)
URL_PREFIX = re.compile(r"^(?:[A-Za-z0-9_-]+/)+$")


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
class PluginIntegration:
    """One extension's complete, lazy executable contribution to HQ."""

    capabilities: Callable[[], Iterable[Any]] | None = None
    resources: Callable[[], Iterable[Any]] | None = None
    connections: Callable[[], Iterable[Any]] | None = None
    dashboard: Callable[[], Iterable[dict[str, Any]]] | None = None
    overview: Callable[[], DomainOverview] | None = None
    attention: Callable[[], Iterable[Any]] | None = None
    search: Callable[[], Iterable[Any]] | None = None
    health: Callable[[], bool] | None = None
    # Calendar sources: each a ``CalendarSource`` naming what the domain
    # already knows the date of.
    calendars: Callable[[], Iterable[Any]] | None = None
    # Work that reaches outside the process: each an ``OutboundWork``. HQ
    # derives its job, capability, route, control and audit entries.
    outbound: Callable[[], Iterable[Any]] | None = None


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    version: str
    distribution: str
    source_repository: str
    source_workflow: str
    api_version: int
    integration_provider: str
    django_apps: tuple[str, ...] = ()
    url_prefix: str = ""
    urlconf: str = ""
    navigation: tuple[NavigationItem, ...] = ()
    # Optional. Routes under this plugin's own url_prefix that carry their own
    # request authentication (a bearer token, a signed body) instead of the
    # session cookie. They are exempted from the session login *redirect*, not
    # from authentication: a 302 to an HTML login page is the wrong answer for
    # a native or machine client, which needs a 401 it can act on.
    #
    # Deliberately relative. The host joins each one to url_prefix, so a plugin
    # can only ever say "these paths of mine": never the host's, never another
    # plugin's mount.
    token_authenticated_routes: tuple[str, ...] = ()
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


def _load_manifest(reference: str) -> PluginManifest:
    """Load identity metadata, translating only manifest-constructor breaks."""

    try:
        manifest = _import(reference)
    except TypeError as exc:
        detail = str(exc)
        if not detail.startswith("PluginManifest.__init__()"):
            raise
        legacy_fields = tuple(
            field for field in PLUGIN_API_1_PROVIDER_FIELDS if field in detail
        )
        if legacy_fields:
            raise ImproperlyConfigured(
                f"Plugin reference {reference!r} uses plugin API 1 provider "
                f"fields ({', '.join(legacy_fields)}); HQ supports "
                f"{PLUGIN_API_VERSION}."
            ) from exc
        if "api_version" in detail and "required" in detail:
            raise ImproperlyConfigured(
                f"Plugin reference {reference!r} does not declare api_version; "
                f"HQ supports {PLUGIN_API_VERSION}."
            ) from exc
        raise
    if not isinstance(manifest, PluginManifest):
        raise ImproperlyConfigured(f"{reference!r} did not expose PluginManifest.")
    return manifest


def _references() -> tuple[str, ...]:
    raw = os.environ.get("SEVERINO_HQ_PLUGINS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _validate_identity(manifest: PluginManifest) -> None:
    """Who the plugin says it is, and whether HQ can run it at all."""

    if not PLUGIN_ID.fullmatch(manifest.id):
        raise ImproperlyConfigured(f"Invalid HQ plugin id {manifest.id!r}.")
    if not manifest.name.strip() or not manifest.version.strip():
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} must declare a name and version."
        )
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


def _validate_mount(manifest: PluginManifest) -> None:
    """Where the plugin attaches to the URL tree, and its Django apps."""

    if bool(manifest.url_prefix) != bool(manifest.urlconf):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} must declare url_prefix and urlconf together."
        )
    if manifest.url_prefix and not URL_PREFIX.fullmatch(manifest.url_prefix):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} has invalid url_prefix {manifest.url_prefix!r}."
        )
    if manifest.urlconf and not PYTHON_PATH.fullmatch(manifest.urlconf):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} has invalid urlconf {manifest.urlconf!r}."
        )
    for app in manifest.django_apps:
        if not PYTHON_PATH.fullmatch(app):
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} has invalid Django app {app!r}."
            )


def _validate_providers(manifest: PluginManifest) -> None:
    """The one executable entry point every extension must declare."""

    if not PYTHON_REFERENCE.fullmatch(manifest.integration_provider):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} has invalid integration_provider "
            f"{manifest.integration_provider!r}."
        )


def _validate_token_routes(manifest: PluginManifest) -> None:
    """Routes exempt from the session login redirect, and only those."""

    if len(manifest.token_authenticated_routes) != len(
        set(manifest.token_authenticated_routes)
    ):
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} repeats a token-authenticated route."
        )
    for route in manifest.token_authenticated_routes:
        if not manifest.url_prefix:
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} declares token-authenticated routes "
                "without a url_prefix to anchor them to."
            )
        # An absolute or traversing route would reach outside the plugin's own
        # mount, which is the one thing this field must never be able to do.
        if not route or route.startswith("/") or ".." in route:
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} token-authenticated route {route!r} "
                "must be a non-empty path relative to its url_prefix."
            )


def _validate_navigation(manifest: PluginManifest) -> None:
    for item in manifest.navigation:
        if (
            not item.label.strip()
            or not item.namespace
            or not item.route
            or not isinstance(item.order, int)
        ):
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} has an incomplete navigation item."
            )


def _validate_capabilities(manifest: PluginManifest) -> None:
    """Names, uniqueness, and the rule that MCP can never exceed the operator."""

    declared_capabilities = (
        *manifest.operator_capabilities,
        *manifest.mcp_read_capabilities,
        *manifest.mcp_write_capabilities,
    )
    for capability in declared_capabilities:
        if not CAPABILITY_NAME.fullmatch(capability):
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} declares invalid capability {capability!r}."
            )
    from .security import host_capabilities

    host = {str(capability) for capability in host_capabilities()}
    claimed = sorted(set(declared_capabilities) & host)
    if claimed:
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} declares the host capability "
            f"{', '.join(claimed)}. A plugin grants only capabilities of its own."
        )
    for label, capabilities in (
        ("operator", manifest.operator_capabilities),
        ("mcp_read", manifest.mcp_read_capabilities),
        ("mcp_write", manifest.mcp_write_capabilities),
    ):
        if len(capabilities) != len(set(capabilities)):
            raise ImproperlyConfigured(
                f"Plugin {manifest.id!r} repeats a capability in {label}."
            )
    operator = set(manifest.operator_capabilities)
    mcp_only = (
        set(manifest.mcp_read_capabilities) | set(manifest.mcp_write_capabilities)
    ) - operator
    if mcp_only:
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} grants MCP capabilities its operator does not "
            f"hold: {', '.join(sorted(mcp_only))}."
        )


def _validate(manifest: PluginManifest) -> None:
    """Everything a manifest must satisfy before HQ will boot with it.

    Grouped rather than tabulated: this is fail-closed startup validation, and
    somebody auditing it should be able to read the requirements in order. A
    table of rules would be shorter and harder to check.

    Order is load-bearing: identity first, because every later message names
    the plugin by the id validated there.
    """

    _validate_identity(manifest)
    _validate_mount(manifest)
    _validate_providers(manifest)
    _validate_token_routes(manifest)
    _validate_navigation(manifest)
    _validate_capabilities(manifest)

def _validate_composition(plugins: tuple[PluginManifest, ...]) -> None:
    """Reject collisions Django would otherwise resolve by ordering."""

    for label, values in (
        ("distribution", [plugin.distribution for plugin in plugins]),
        ("Django app", [app for plugin in plugins for app in plugin.django_apps]),
        (
            "URL prefix",
            [plugin.url_prefix for plugin in plugins if plugin.url_prefix],
        ),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ImproperlyConfigured(
                f"Duplicate plugin {label}: {', '.join(duplicates)}."
            )

    prefixes = sorted(
        plugin.url_prefix for plugin in plugins if plugin.url_prefix
    )
    for index, prefix in enumerate(prefixes):
        nested = next(
            (candidate for candidate in prefixes[index + 1 :] if candidate.startswith(prefix)),
            None,
        )
        if nested:
            raise ImproperlyConfigured(
                f"Plugin URL prefixes overlap: {prefix!r} and {nested!r}."
            )


@cache
def _installed_plugins() -> tuple[PluginManifest, ...]:
    plugins = []
    ids = set()
    for reference in _references():
        manifest = _load_manifest(reference)
        _validate(manifest)
        if manifest.id in ids:
            raise ImproperlyConfigured(f"Duplicate HQ plugin id {manifest.id!r}.")
        ids.add(manifest.id)
        plugins.append(manifest)
    installed = tuple(sorted(plugins, key=lambda item: item.id))
    _validate_composition(installed)
    from .plugin_admission import enforce_plugin_admission

    enforce_plugin_admission(installed)
    return installed


def installed_plugins() -> tuple[PluginManifest, ...]:
    """The admitted manifest set for this immutable process composition."""

    return _installed_plugins()


def clear_plugin_composition_cache() -> None:
    """Forget plugin identity and every graph derived from that identity."""

    _installed_plugins.cache_clear()
    # A question asked of the composition that was is not asked of this one.
    forget()
    from .integrations import clear_integration_graph_cache
    from .outbound_work import clear_outbound_work_cache

    clear_integration_graph_cache()
    clear_outbound_work_cache()


def installed_plugin_apps() -> list[str]:
    return [app for plugin in installed_plugins() for app in plugin.django_apps]


def plugin_urlpatterns() -> list[URLResolver]:
    mounted = [plugin for plugin in installed_plugins() if plugin.urlconf]
    return [path(plugin.url_prefix, include(plugin.urlconf)) for plugin in mounted]


def plugin_token_authenticated_prefixes() -> tuple[str, ...]:
    """Absolute path prefixes that authenticate themselves, not via the session.

    Built here rather than declared, so what a plugin asked for is always
    re-anchored to the mount the host gave it.
    """

    return tuple(
        f"/{plugin.url_prefix}{route}"
        for plugin in installed_plugins()
        for route in plugin.token_authenticated_routes
    )


def installed_integrations() -> tuple[tuple[PluginManifest, PluginIntegration], ...]:
    """Pair installed identity with its typed, query-lazy contribution."""

    integrations = []
    for plugin in installed_plugins():
        provider: Callable[[], PluginIntegration] = _import(
            plugin.integration_provider
        )
        integration = provider()
        if not isinstance(integration, PluginIntegration):
            raise ImproperlyConfigured(
                f"Plugin {plugin.id!r} integration provider must return "
                "PluginIntegration."
            )
        invalid = [
            field
            for field in (
                "capabilities",
                "resources",
                "connections",
                "dashboard",
                "overview",
                "attention",
                "search",
                "health",
                "calendars",
                "outbound",
            )
            if (value := getattr(integration, field)) is not None
            and not callable(value)
        ]
        if invalid:
            raise ImproperlyConfigured(
                f"Plugin {plugin.id!r} integration fields must be callable: "
                f"{', '.join(invalid)}."
            )
        integrations.append((plugin, _derived(plugin.id, integration)))
    return tuple(integrations)


# ----- An extension's part of a shared composition, derived once per change ---
#
# What an extension contributes to a page many domains share (the queue, the
# dashboard, the calendar) is asked for on every request that composes one. The
# host therefore asks through a derivation of its own, named for the plugin
# and the provider: the answer is kept until a table the provider was seen to
# read is written, and an extension is cached without knowing a cache exists.
#
# The host cannot see where a provider reads the clock, so each is ``unseen``:
# its answer stands a minute at most and never past local midnight. It varies
# by the one thing a provider is given that is not a row or the clock: whether
# the reader asked for a demo.

# The providers answered with one value for everybody, by field.
DERIVED_PROVIDERS = ("attention", "dashboard", "overview")
# How many windows of one calendar source are kept current: the one the
# dashboard shows and the one last paged to.
CALENDAR_WINDOWS = 2

_PROVIDERS: dict[str, Callable[..., Any]] = {}
_ANSWERS: dict[str, Callable[..., Any]] = {}


# This process, and each provider met in it that nothing else tells apart.
_PROCESS = time.time_ns()
_UNNAMED: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_SERIAL = itertools.count(1)


def _itself(provider: Any) -> str:
    """A provider known only as the object it is, and only in this process."""

    try:
        serial = _UNNAMED.get(provider)
        if serial is None:
            serial = _UNNAMED[provider] = next(_SERIAL)
    except TypeError:
        # Not weakly referable: its address names it while it lives.
        return f"{_PROCESS}:at:{id(provider)}"
    return f"{_PROCESS}:{serial}"


def _holding(values: Any) -> str | None:
    """What a provider was built holding, as a digest; None when it cannot be told."""

    try:
        return hashlib.sha256(pickle.dumps(values)).hexdigest()[:16]
    except Exception:  # noqa: BLE001 - whatever cannot be pickled cannot be compared
        return None


@lru_cache(maxsize=1024)
def _written(code: CodeType) -> str:
    """A function's own code as a digest: two functions of one name differ by it."""

    return hashlib.sha256(marshal.dumps(code)).hexdigest()[:16]


def _identity(provider: Callable[..., Any]) -> str:
    """Which function answers, so one provider is never answered with what
    another stored.

    A function is named by where it is defined and what it is written to do,
    which every process running one release agrees on, and by what it was
    built holding: a closure made again on every call is the same provider
    while it holds the same things. Anything that cannot be told apart that
    way is known only as itself.
    """

    if isinstance(provider, partial):
        held = _holding((provider.args, sorted(provider.keywords.items())))
        return _itself(provider) if held is None else f"{_identity(provider.func)}({held})"
    if not inspect.isfunction(provider):
        return _itself(provider)
    name = f"{provider.__module__}:{provider.__qualname__}:{_written(provider.__code__)}"
    if provider.__closure__ is None:
        return name
    try:
        held = _holding(tuple(cell.cell_contents for cell in provider.__closure__))
    except ValueError:
        # A cell not yet filled.
        held = None
    return _itself(provider) if held is None else f"{name}[{held}]"


def answered_by(provider: Callable[..., Any]) -> str:
    """Which function answers when ``provider`` is asked: the one registered
    behind a derived provider, the provider itself otherwise."""

    for name, answer in _ANSWERS.items():
        if answer is provider:
            return _identity(_PROVIDERS[name])
    return _identity(provider)


def _kept(value: Any) -> Any:
    """What a provider returned, as a value that can be stored: a generator's
    items, anything else as it is."""

    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        return value
    return tuple(value)


def provided(name: str, provider: Callable[..., Any], *, ahead: int | None = None) -> Callable[..., Any]:
    """``provider`` asked through the derivation ``name``, declared on first use.

    The derivation outlives one composition: it answers with whichever
    provider is registered under its name now.
    """

    _PROVIDERS[name] = provider
    answer = _ANSWERS.get(name)
    if answer is None:

        def vary(*args: Any) -> tuple[Any, ...]:
            return (*args, showing_demo(), _identity(_PROVIDERS[name]))

        def compute(*args: Any) -> Any:
            return _kept(_PROVIDERS[name](*args))

        declare = (
            derivation(name, reads=(), vary=vary, unseen=True)
            if ahead is None
            else derivation(name, reads=(), vary=vary, unseen=True, ahead=ahead)
        )
        answer = _ANSWERS[name] = declare(compute)
    return answer


def _is_source(source: Any) -> bool:
    """Whether this is a calendar source to wrap; anything else is the
    calendar's to refuse, by name."""

    return is_dataclass(source) and not isinstance(source, type) and callable(getattr(source, "events", None))


def _derived_calendars(plugin_id: str, provider: Callable[[], Iterable[Any]]) -> Callable[[], tuple[Any, ...]]:
    """The provider's sources, each reading its window through a derivation."""

    def derived(source: Any) -> Any:
        if not _is_source(source):
            return source
        events = provided(
            f"extension.{plugin_id}.calendar.{source.id}", source.events, ahead=CALENDAR_WINDOWS
        )
        return replace(source, events=events)

    def sources() -> tuple[Any, ...]:
        return tuple(derived(source) for source in provider())

    return sources


def _derived(plugin_id: str, integration: PluginIntegration) -> PluginIntegration:
    """``integration`` with each provider of a shared composition derived."""

    derived: dict[str, Any] = {
        field: provided(f"extension.{plugin_id}.{field}", provider)
        for field in DERIVED_PROVIDERS
        if (provider := getattr(integration, field)) is not None
    }
    if integration.calendars is not None:
        derived["calendars"] = _derived_calendars(plugin_id, integration.calendars)
    return replace(integration, **derived)


def _provided(attribute: str) -> tuple[Any, ...]:
    return tuple(
        value
        for _, integration in installed_integrations()
        if (provider := getattr(integration, attribute)) is not None
        for value in provider()
    )


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
    for plugin, integration in installed_integrations():
        if integration.dashboard is None:
            continue
        cards = tuple(integration.dashboard())
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


def gather_cards(
    sources: Iterable[tuple[str, Callable[[], Iterable[dict[str, Any]]] | None]],
) -> tuple[dict[str, Any], ...]:
    """Flatten and validate dashboard cards from any ``(id, provider)`` sources.

    The counterpart to ``gather_attention``: one validator serves the host's own
    sections and installed extensions, so a malformed card fails the same way
    whoever produced it.
    """

    cards = tuple(
        card
        for _, provider in sources
        if provider is not None
        for card in provider()
    )
    _validate_dashboard_cards(cards)
    ids = [card["id"] for card in cards]
    if len(ids) != len(set(ids)):
        raise ImproperlyConfigured(
            "Duplicate dashboard card id across HQ sections and extensions."
        )
    return cards


def plugin_overviews() -> tuple[dict[str, Any], ...]:
    """Typed rich overviews with attribution owned by the host registry."""
    sections = []
    for plugin, integration in installed_integrations():
        if integration.overview is None:
            continue
        overview = integration.overview()
        if not isinstance(overview, DomainOverview):
            raise ImproperlyConfigured(
                f"Plugin {plugin.id!r} overview must return DomainOverview."
            )
        sections.append({"id": plugin.id, "label": plugin.name, "overview": overview})
    return tuple(sections)


def _validate_dashboard_cards(cards: Iterable[object]) -> None:
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


def gather_attention(
    sources: Iterable[
        tuple[str, str, Callable[[], Iterable[Any]] | None]
    ],
) -> tuple[dict[str, Any], ...]:
    """Collect, validate, attribute and order attention items from any sources.

    ``sources`` is ``(id, label, provider)`` per contributor, which is
    all this needs to know, so the host's own sections and installed
    extensions are gathered by one implementation rather than two that could
    drift on what "urgent" means or on which statuses count.

    Each entry carries its source so a composing surface can say where an item
    came from without the contributor restating its own name. Ordering is a
    property of the status vocabulary rather than of the caller, so every
    surface that renders these agrees on urgency instead of re-sorting to taste.
    """

    gathered: list[dict[str, Any]] = []
    for source_id, label, provider in sources:
        if provider is None:
            continue
        for item in provider():
            status = getattr(item, "status", None)
            if status not in STATUS_VALUES:
                raise ImproperlyConfigured(
                    f"{source_id!r} produced an attention item with status "
                    f"{status!r}; expected one of {', '.join(sorted(STATUS_VALUES))}."
                )
            if status not in ATTENTION_ORDER:
                continue
            gathered.append({"item": item, "source": label, "source_id": source_id})
    return ordered_attention(gathered)


def ordered_attention(entries: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Gathered entries in the one order every surface shows them."""

    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                ATTENTION_ORDER.index(entry["item"].status),
                entry["source"],
                entry["item"].title,
            ),
        )
    )


def plugin_attention_items() -> tuple[dict[str, Any], ...]:
    """What every installed extension believes needs a decision now.

    Extensions only. This is the SDK surface a composing extension uses to
    render its sibling domains; HQ's own dashboard composes *every* domain and
    calls ``application.domains.domain_attention_items`` instead.

    Takes no exclusions: the queue is the one place a `serious` item is
    guaranteed to appear, so nothing may remove a domain from it.
    A composer that also renders per-domain panels should group these by
    `source_id` rather than fetch the same question from a second channel.
    """

    # A notice reports something and asks for nothing, so it is not a
    # decision: it is in the queue's own section and nowhere under this name.
    return tuple(
        entry
        for entry in gather_attention(
            (plugin.id, plugin.name, integration.attention)
            for plugin, integration in installed_integrations()
        )
        if not getattr(entry["item"], "notice", False)
    )


def plugin_outbound_work() -> tuple[Any, ...]:
    return _provided("outbound")


def plugin_capability_specs() -> tuple[Any, ...]:
    """What every extension declares, and the capability each piece of its
    outbound work is asked for through."""

    from .outbound_work import capability_for

    return (
        *_provided("capabilities"),
        *(capability_for(work) for work in plugin_outbound_work()),
    )


def plugin_resource_specs() -> tuple[Any, ...]:
    return _provided("resources")


def plugin_connection_specs() -> tuple[Any, ...]:
    return _provided("connections")


def plugin_search_definitions() -> tuple[Any, ...]:
    return _provided("search")


def plugin_health() -> dict[str, bool]:
    return {
        plugin.id: bool(integration.health()) if integration.health else True
        for plugin, integration in installed_integrations()
    }


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
