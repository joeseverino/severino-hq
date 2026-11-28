"""Versioned, explicitly allowlisted extension contract for trusted HQ plugins."""

import logging
import os
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from importlib import import_module
from typing import TYPE_CHECKING, Any, get_args, get_origin

from django.core.exceptions import ImproperlyConfigured

from hq.platform.application.routes import reverse

from .card_items import Counted
from .composed_providers import DERIVED_PROVIDERS, derived_calendars, provided, provided_under
from .derivations import forget, holds_until, present
from .lists import split_list
from .non_empty import RequiredText, holds_something
from .tone import Tone
from .ui import DomainOverview
lazy from . import finding_model, finding_steps
lazy from .action_items import item_key
lazy from .findings import finding_key
lazy from .host_sections import NAVIGATION_GROUPS
lazy from .plugin_admission import enforce_plugin_admission
lazy from .security import host_capabilities

if TYPE_CHECKING:
    from .finding_model import Finding
    from .freshness import ReadCadence

logger = logging.getLogger("severino.composition")

PLUGIN_API_VERSION = 33

PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_]*)+$")
PLUGIN_DISTRIBUTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLUGIN_WORKFLOW = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
PYTHON_PATH = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
PYTHON_REFERENCE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
URL_PREFIX = re.compile(r"^(?:[A-Za-z0-9_-]+/)+$")


Check = tuple[str, Callable[[Any, Any], bool], str]


class DeclarationRefused(ImproperlyConfigured):
    """Every field one declaration gets wrong, each with what is wrong there."""

    def __init__(self, subject: str, violations: list[tuple[str, str]]) -> None:
        self.violations = tuple(violations)
        lines = "".join(f"\n  {field}: {message}" for field, message in self.violations)
        super().__init__(f"{subject} is refused, field by field:{lines}")


def _declared_as(value: object, kind: Any) -> bool:
    """Whether ``value`` has a field's declared type as written: a list is no tuple, a bool no int."""

    if get_origin(kind) is tuple:
        item = get_args(kind)[0]
        return type(value) is tuple and all(_declared_as(part, item) for part in value)
    return isinstance(value, kind) and not (kind is int and isinstance(value, bool))


def _declared_types(cls: Any) -> dict[str, Any]:
    return {field.name: field.type for field in fields(cls)}


def _violations(declaration: object, checks: tuple[Check, ...]) -> list[tuple[str, str]]:
    """Every field of ``declaration`` of the wrong type, then every check it fails on a field typed right.

    A check is ``(field, holds, message)``: ``holds`` is given the field's
    value and the whole declaration, and ``message`` is formatted with the value.
    """

    declared = _declared_types(type(declaration))
    wrong = [name for name, kind in declared.items() if not _declared_as(getattr(declaration, name), kind)]
    found = [(name, f"is not {_type_name(declared[name])}: {getattr(declaration, name)!r}") for name in wrong]
    for name, holds, message in checks:
        value = getattr(declaration, name)
        if name not in wrong and not holds(value, declaration):
            found.append((name, message.format(value=value)))
    return found


def _type_name(kind: Any) -> str:
    if get_origin(kind) is tuple:
        return f"a tuple, each item {_type_name(get_args(kind)[0])}"
    return {str: "a string", int: "an integer"}.get(kind, f"a {kind.__name__}")


def _matches(pattern: re.Pattern[str]) -> Callable[[str, Any], bool]:
    return lambda value, _: pattern.fullmatch(value) is not None


def _each_matches(pattern: re.Pattern[str]) -> Callable[[tuple[str, ...], Any], bool]:
    return lambda values, _: all(pattern.fullmatch(value) for value in values)


def _filled(value: str, _: Any) -> bool:
    return bool(value.strip())


def _distinct(values: tuple[str, ...], _: Any) -> bool:
    return len(values) == len(set(values))


@dataclass(frozen=True, slots=True)
class NavigationItem:
    label: str
    route: str
    # Empty only for a host page outside any URL namespace; a plugin's item
    # names its own (``PluginManifest.navigation``).
    namespace: str
    order: int = 500
    # Empty renders inline in the primary nav; a name collects this item into
    # the matching dropdown. Defaults to inline so a surface stays reachable in
    # one click, and so existing manifests keep their current placement.
    group: str = ""

    def __post_init__(self) -> None:
        if violations := _violations(self, NAVIGATION_CHECKS):
            raise DeclarationRefused(f"Navigation item {self.label!r}", violations)


NAVIGATION_CHECKS: tuple[Check, ...] = (
    ("label", _filled, "is blank"),
    ("route", lambda route, _: bool(route), "is empty"),
)


@dataclass(frozen=True, slots=True)
class NavigationGroup:
    """A nav group an extension adds, and what it is for.

    ``name`` is the ``group`` its items give; ``description`` is the sentence a
    reader of the machine contract reads under the group's name, as the host's
    own groups say theirs (``host_sections.NAVIGATION_GROUPS``).
    """

    name: RequiredText
    description: RequiredText

    def __post_init__(self) -> None:
        holds_something(self)


@dataclass(frozen=True, slots=True)
class PluginIntegration:
    """One extension's complete, lazy executable contribution to HQ."""

    capabilities: Callable[[], Iterable[Any]] | None = None
    resources: Callable[[], Iterable[Any]] | None = None
    connections: Callable[[], Iterable[Any]] | None = None
    dashboard: Callable[[], Iterable[dict[str, Any]]] | None = None
    overview: Callable[[], DomainOverview] | None = None
    # What needs a person, each a finding the host words as its queue row.
    attention: Callable[[], Iterable[Finding]] | None = None
    # The page where ``attention``'s items are decided together, when there
    # is one: the queue's heading for the domain links there.
    attention_url: Callable[[], str] | None = None
    search: Callable[[], Iterable[Any]] | None = None
    # Whether the extension can serve at all; readiness ANDs every answer. A
    # missing secret or setting is shown through ``attention``, never as false.
    health: Callable[[], bool] | None = None
    # Calendar sources: each a ``CalendarSource`` naming what the domain
    # already knows the date of.
    calendars: Callable[[], Iterable[Any]] | None = None
    # Work that reaches outside the process: each an ``OutboundWork``. HQ
    # derives its job, capability, route, control and audit entries.
    outbound: Callable[[], Iterable[Any]] | None = None
    # How old a reading of each of the extension's kinds may get, by kind:
    # ``hq_sdk.reads.freshness`` and the Connections page judge it by this.
    # Each kind's is asked only as that kind is judged, so it may follow a
    # setting without the host reading it for any other kind.
    cadences: Callable[[], Mapping[str, Callable[[], ReadCadence]]] | None = None
    # The extension's demo records, written for the user it is given into a
    # database the example estate was just written to, by the showcase and
    # standard data sets alone (``hq.platform.core.seeds``). Its dates sit
    # relative to today and its keys are fixed, so every database it writes
    # holds the same pages.
    demo_seed: Callable[[Any], object] | None = None
    # A record's page of each kind ``demo_seed`` writes, by address, which the
    # layout gate and dev:look's ``all`` read beside the host's own.
    demo_pages: Callable[[], Iterable[str]] | None = None
    # Workflows that cross outside systems: each a ``SequenceSpec`` whose
    # steps every surface draws and reads (``application.sequences``).
    sequences: Callable[[], Iterable[Any]] | None = None


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Who an extension is and what it mounts, checked whole where it is built.

    The extension's import builds it, so one ``DeclarationRefused`` there names
    every field ``MANIFEST_CHECKS`` refuses. What needs the running host (its
    own capabilities, the rest of the installed set) is checked at boot, in
    ``_admitted``.
    """

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
    operator_capabilities: tuple[str, ...] = ()
    mcp_read_capabilities: tuple[str, ...] = ()
    # What the domain holds, in a sentence a reader of HQ's machine contract
    # reads under its name, as a host section's ``DomainDescriptor.description``.
    description: str = ""
    # What each nav group this manifest's items add is for.
    navigation_groups: tuple[NavigationGroup, ...] = ()

    def __post_init__(self) -> None:
        if violations := _violations(self, MANIFEST_CHECKS):
            raise DeclarationRefused(f"Plugin {self.id!r}", violations)


def _held_by_its_operator(granted: tuple[str, ...], manifest: PluginManifest) -> bool:
    operator = manifest.operator_capabilities
    return not _declared_as(operator, tuple[str, ...]) or set(granted) <= set(operator)


MANIFEST_CHECKS: tuple[Check, ...] = (
    ("id", _matches(PLUGIN_ID), "is not a plugin id such as example.notes"),
    ("name", _filled, "is blank"),
    ("version", _filled, "is blank"),
    ("distribution", _matches(PLUGIN_DISTRIBUTION), "is not a distribution name: {value!r}"),
    ("source_repository", _matches(PLUGIN_REPOSITORY), "is not an owner/name repository: {value!r}"),
    ("source_workflow", _matches(PLUGIN_WORKFLOW), "is not a workflow under .github/workflows: {value!r}"),
    (
        "api_version",
        lambda version, _: version == PLUGIN_API_VERSION,
        f"requires API {{value}}; HQ supports {PLUGIN_API_VERSION}",
    ),
    ("integration_provider", _matches(PYTHON_REFERENCE), "is not a module:attribute reference: {value!r}"),
    ("django_apps", _each_matches(PYTHON_PATH), "names an app that is not a module path: {value!r}"),
    (
        "url_prefix",
        lambda prefix, _: not prefix or URL_PREFIX.fullmatch(prefix) is not None,
        "is not a relative path ending in /: {value!r}",
    ),
    (
        "urlconf",
        lambda urlconf, _: not urlconf or PYTHON_PATH.fullmatch(urlconf) is not None,
        "is not a module path: {value!r}",
    ),
    (
        "urlconf",
        lambda urlconf, manifest: bool(urlconf) == bool(manifest.url_prefix),
        "is set with url_prefix, or neither is",
    ),
    ("navigation", lambda items, _: all(item.namespace for item in items), "has an item that names no namespace"),
    (
        "navigation_groups",
        lambda groups, manifest: {group.name for group in groups} <= {item.group for item in manifest.navigation},
        "says what a group is for that none of its navigation items is in",
    ),
    (
        "navigation_groups",
        lambda groups, _: _distinct(tuple(group.name for group in groups), _),
        "says what one group is for twice",
    ),
    *(
        check
        for name in ("operator_capabilities", "mcp_read_capabilities")
        for check in (
            (name, _each_matches(CAPABILITY_NAME), "names one that is not a dotted lowercase name: {value!r}"),
            (name, _distinct, "repeats one: {value!r}"),
        )
    ),
    ("mcp_read_capabilities", _held_by_its_operator, "grants MCP what its operator does not hold: {value!r}"),
)


def import_reference(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ImproperlyConfigured(f"Plugin reference {spec!r} must use 'module:attribute'.")
    try:
        return getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise ImproperlyConfigured(f"Cannot load HQ plugin reference {spec!r}.") from exc


def _load_manifest(reference: str) -> PluginManifest:
    manifest = import_reference(reference)
    if not isinstance(manifest, PluginManifest):
        raise ImproperlyConfigured(f"{reference!r} did not expose PluginManifest.")
    return manifest


def _references() -> tuple[str, ...]:
    raw = os.environ.get("SEVERINO_HQ_PLUGINS", "")
    return tuple(split_list(raw))


def _claims_no_host_capability(manifest: PluginManifest) -> None:
    """A plugin grants only capabilities of its own, which needs the host's to tell."""

    declared = {*manifest.operator_capabilities, *manifest.mcp_read_capabilities}
    claimed = sorted(declared & {str(capability) for capability in host_capabilities()})
    if claimed:
        raise ImproperlyConfigured(
            f"Plugin {manifest.id!r} declares the host capability "
            f"{', '.join(claimed)}. A plugin grants only capabilities of its own."
        )


def _validate_composition(plugins: tuple[PluginManifest, ...]) -> None:
    """Reject collisions Django would otherwise resolve by ordering."""

    for label, values in (
        ("distribution", [plugin.distribution for plugin in plugins]),
        ("Django app", [app for plugin in plugins for app in plugin.django_apps]),
        # A group says what it is for once: the host's, or one extension's.
        (
            "navigation group",
            [*NAVIGATION_GROUPS, *(group.name for plugin in plugins for group in plugin.navigation_groups)],
        ),
        (
            "URL prefix",
            [plugin.url_prefix for plugin in plugins if plugin.url_prefix],
        ),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise ImproperlyConfigured(f"Duplicate plugin {label}: {', '.join(duplicates)}.")

    prefixes = sorted(plugin.url_prefix for plugin in plugins if plugin.url_prefix)
    for index, prefix in enumerate(prefixes):
        nested = next(
            (candidate for candidate in prefixes[index + 1 :] if candidate.startswith(prefix)),
            None,
        )
        if nested:
            raise ImproperlyConfigured(f"Plugin URL prefixes overlap: {prefix!r} and {nested!r}.")


def _admitted() -> tuple[PluginManifest, ...]:
    plugins = []
    ids = set()
    for reference in _references():
        manifest = _load_manifest(reference)
        _claims_no_host_capability(manifest)
        if manifest.id in ids:
            raise ImproperlyConfigured(f"Duplicate HQ plugin id {manifest.id!r}.")
        ids.add(manifest.id)
        plugins.append(manifest)
    installed = tuple(sorted(plugins, key=lambda item: item.id))
    _validate_composition(installed)

    enforce_plugin_admission(installed)
    return installed


def _named(reference: str) -> tuple[object, object]:
    """The module ``reference`` names and its attribute, None before it is imported."""

    name, _, attribute = reference.partition(":")
    module = sys.modules.get(name)
    return module, getattr(module, attribute, None)


def _read_from() -> tuple[object, ...]:
    """What the composition is read from: its references, and each module and
    object they name, a manifest's integration provider included."""

    references = _references()
    named = tuple(_named(reference) for reference in references)
    providers = (manifest.integration_provider for _, manifest in named if isinstance(manifest, PluginManifest))
    return (references, *named, *map(_named, providers))


@dataclass(frozen=True, eq=False)
class Composition:
    """The admitted set and what it was read from: what is derived from the set
    is cached by it, so a change of its inputs is a new key, never a stale answer."""

    read_from: tuple[object, ...]
    plugins: tuple[PluginManifest, ...]


_COMPOSED: list[Composition] = []
# Held while the set is admitted, so threads asking at once compose it once.
_COMPOSING = threading.Lock()


def composition() -> Composition:
    """The composition as its inputs stand now, admitted again once one changes."""

    if _COMPOSED and _COMPOSED[0].read_from == _read_from():
        return _COMPOSED[0]
    with _COMPOSING:
        if _COMPOSED and _COMPOSED[0].read_from == _read_from():
            return _COMPOSED[0]
        plugins = _admitted()
        # Read after admitting, which imports what the references name.
        _COMPOSED[:] = [Composition(_read_from(), plugins)]
        # A question asked of the composition that was is not asked of this one.
        forget()
        return _COMPOSED[0]


def installed_plugins() -> tuple[PluginManifest, ...]:
    """The admitted manifest set for this process's composition."""

    return composition().plugins


def installed_plugin_apps() -> list[str]:
    return [app for plugin in installed_plugins() for app in plugin.django_apps]


def installed_integrations() -> tuple[tuple[PluginManifest, PluginIntegration], ...]:
    """Pair installed identity with its typed, query-lazy contribution."""

    integrations = []
    for plugin in installed_plugins():
        provider: Callable[[], PluginIntegration] = import_reference(plugin.integration_provider)
        integration = provider()
        if not isinstance(integration, PluginIntegration):
            raise ImproperlyConfigured(f"Plugin {plugin.id!r} integration provider must return PluginIntegration.")
        # Every field is a provider, so each one is held to being callable.
        invalid = [
            field.name
            for field in fields(integration)
            if (value := getattr(integration, field.name)) is not None and not callable(value)
        ]
        if invalid:
            raise ImproperlyConfigured(
                f"Plugin {plugin.id!r} integration fields must be callable: {', '.join(invalid)}."
            )
        integrations.append((plugin, _derived(plugin.id, integration)))
    return tuple(integrations)


def _derived(plugin_id: str, integration: PluginIntegration) -> PluginIntegration:
    """``integration`` with each provider of a shared composition derived."""

    derived: dict[str, Any] = {
        field: provided(f"{provided_under(plugin_id)}.{field}", provider)
        for field in DERIVED_PROVIDERS
        if (provider := getattr(integration, field)) is not None
    }
    if integration.calendars is not None:
        derived["calendars"] = derived_calendars(plugin_id, integration.calendars)
    return replace(integration, **derived)


def _provided(attribute: str) -> tuple[Any, ...]:
    return tuple(
        value
        for _, integration in installed_integrations()
        if (provider := getattr(integration, attribute)) is not None
        for value in provider()
    )


def read_source[T](source_id: str, ask: Callable[[], T], unreadable: Callable[[], T]) -> T:
    """What one source of a composed surface answers, or ``unreadable()`` in its place when asking raises.

    Every page, queue and snapshot composes many sources, and one that fails
    must not take the rest down with it. The stand-in holds for no time, so no
    derivation that composed it is stored and the next ask tries the source
    again; the failure is logged with its traceback under the request's id.
    """

    try:
        return ask()
    except Exception:  # whatever one source raises is that source's failure alone
        logger.exception("%s could not be read.", source_id, extra={"event": "composition.source.unreadable"})
        holds_until(present())
        return unreadable()


# What a source that could not be read is raised under, keyed by the source.
UNREADABLE = "unreadable"


def source_attention(
    source_id: str, label: str, provider: Callable[[], Iterable[Any]], read_capabilities: tuple[str, ...] = ()
) -> tuple[Any, ...]:
    """A source's attention items, or one item saying it could not be read.

    The stand-in is gated on ``read_capabilities``, what a reader of the
    source holds, so it tells no reader more than the source would.
    """

    return read_source(
        source_id,
        lambda: distinct_attention(source_id, checked_attention(source_id, tuple(provider()))),
        lambda: (
            finding_model.Finding(
                rule=UNREADABLE,
                subject=source_id,
                severity=Tone.ATTENTION,
                title=f"{label} could not be read",
                explanation="What it reports is missing from this queue until it reads again. The failure is logged.",
                why="Asking this source for its items raised an error, so anything it would report is missing.",
                fixes=(
                    finding_steps.Do(
                        f"Find the line in HQ's log with this event whose message names {source_id}; the traceback "
                        "under it says what failed.",
                        value="composition.source.unreadable",
                    ),
                ),
                read_capabilities=read_capabilities,
            ),
        ),
    )


def source_cards(
    source_id: str, label: str, url: str, provider: Callable[[], Iterable[dict[str, Any]]]
) -> tuple[dict[str, Any], ...]:
    """A source's dashboard cards, or one card saying it could not be read."""

    return read_source(
        source_id,
        lambda: checked_cards(source_id, tuple(provider())),
        lambda: (
            {
                "id": f"{source_id}.unreadable",
                "label": label,
                "value": "Unreadable",
                "url": url,
                "detail": "Could not be read",
                "status": Tone.ATTENTION,
            },
        ),
    )


def source_overview(source_id: str, provider: Callable[[], Any]) -> DomainOverview | None:
    """A source's overview; None for none, or for one that could not be read,
    whose section its cards then fill."""

    return read_source(source_id, lambda: checked_overview(source_id, provider()), lambda: None)


# What one source's answer must be, by the provider that gives it: a source
# whose answer is not is that source's failure (``read_source``), and the
# extension kit names it (``plugin_testing.providers_derived_again``).
def checked_attention(source_id: str, items: tuple[Any, ...]) -> tuple[Finding, ...]:
    for item in items:
        if not isinstance(item, finding_model.Finding):
            raise ImproperlyConfigured(f"{source_id!r} produced an attention item that is not a Finding: {item!r}.")
        # An item the queue shows is never a problem alone: a step or remedy,
        # the page the work is done on, the reason HQ cannot help, or a held
        # request's decision.
        helped = item.steps or item.fixes or item.primary or item.no_help_reason.strip() or item.held
        if item.severity.acted_on and not helped:
            raise ImproperlyConfigured(f"{source_id!r} produced an attention item with no help: {item.title!r}.")
    return items


def repeated_keys(source_id: str, items: tuple[Finding, ...]) -> tuple[str, ...]:
    """Each queue key (``action_items.item_key``) more than one of a source's items share."""

    counts = Counter(item_key(source_id, finding_key(item)) for item in items)
    return tuple(key for key, count in counts.items() if count > 1)


def distinct_attention(source_id: str, items: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """A source's items, one per queue key: the worst of each, where it stood.

    A key is what the queue knows an item by (its row, its dismissal), so two
    items under one are one to every reader. The source is still read: one
    repeat in live data must not hide the source's other items, so the rest
    of a key's items are dropped and logged as the source's defect.
    """

    worst: dict[str, Finding] = {}
    for item in items:
        key = item_key(source_id, finding_key(item))
        held = worst.get(key)
        if held is None or _urgency(item) < _urgency(held):
            worst[key] = item
    if len(worst) < len(items):
        logger.error(
            "%s gave more than one attention item under %s; the queue keeps the worst of each.",
            source_id,
            ", ".join(repeated_keys(source_id, items)),
            extra={"event": "composition.source.repeated_key"},
        )
    kept = {id(item) for item in worst.values()}
    return tuple(item for item in items if id(item) in kept)


def _urgency(item: Finding) -> tuple[int, bool]:
    return item.severity.rank, item.notice is not None


def _refused_attention(source_id: str, items: tuple[Any, ...]) -> tuple[Any, ...]:
    """What a source's own suite holds it to: no two items under one key."""

    checked = checked_attention(source_id, items)
    if repeated := repeated_keys(source_id, checked):
        raise ImproperlyConfigured(f"{source_id!r} produced two attention items with one key: {', '.join(repeated)}.")
    return checked


def checked_cards(source_id: str, cards: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
    """``cards`` held to the card contract, each count said in its words: its
    label agreeing with its value, as every surface and transport reads it."""

    _validate_dashboard_cards(cards)
    ids = [card["id"] for card in cards]
    if len(ids) != len(set(ids)):
        raise ImproperlyConfigured(f"{source_id!r} produced two dashboard cards with one id.")
    return tuple(
        card | {"label": card["label"].words, "value": card["label"].count}
        if isinstance(card["label"], Counted)
        else card
        for card in cards
    )


def checked_overview(source_id: str, overview: Any) -> DomainOverview | None:
    if overview is not None and not isinstance(overview, DomainOverview):
        raise ImproperlyConfigured(f"{source_id!r} overview must return DomainOverview.")
    return overview


SOURCE_CHECKS: dict[str, Callable[[str, Any], Any]] = {
    "attention": lambda source_id, answer: _refused_attention(source_id, tuple(answer)),
    "dashboard": lambda source_id, answer: checked_cards(source_id, tuple(answer)),
    "overview": checked_overview,
}


# A card is a scalar and a link, plus optional interpretation. Kept deliberately
# small: the aggregate is rendered by surfaces that compose many extensions at
# once, so a card must be cheap to produce and safe to show beside any other.
# A card that counts things gives a ``Counted`` as its label and no value, so a
# line that sets the label after the count says "1 Draft", never "1 Drafts".
CARD_REQUIRED_KEYS = frozenset({"id", "label", "url"})
CARD_OPTIONAL_KEYS = frozenset({"value", "detail", "status", "trend"})
# Direction of travel, not a judgment: whether "up" is good is the domain's
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
        home = reverse(plugin.navigation[0].route) if plugin.navigation else ""
        cards = source_cards(plugin.id, plugin.name, home, integration.dashboard)
        sections.append(
            {"id": plugin.id, "label": plugin.name, "url": home or (cards[0]["url"] if cards else ""), "cards": cards}
        )
    return tuple(sections)


def gather_cards(
    sources: Iterable[tuple[str, Callable[[], Iterable[dict[str, Any]]] | None]],
) -> tuple[dict[str, Any], ...]:
    """Flatten dashboard cards from any ``(id, provider)`` sources, each already
    read through ``source_cards``, refusing an id two of them share: no one
    source owns that failure.
    """

    cards = tuple(card for _, provider in sources if provider is not None for card in provider())
    ids = [card["id"] for card in cards]
    if len(ids) != len(set(ids)):
        raise ImproperlyConfigured("Duplicate dashboard card id across HQ sections and extensions.")
    return cards


def plugin_overviews() -> tuple[dict[str, Any], ...]:
    """Typed rich overviews with attribution owned by the host registry."""
    sections = []
    for plugin, integration in installed_integrations():
        if integration.overview is None:
            continue
        overview = source_overview(plugin.id, integration.overview)
        if overview is None:
            continue
        sections.append({"id": plugin.id, "label": plugin.name, "overview": overview})
    return tuple(sections)


def _validate_dashboard_cards(cards: Iterable[object]) -> None:
    for card in cards:
        if not isinstance(card, dict) or not card.keys() >= CARD_REQUIRED_KEYS:
            raise ImproperlyConfigured("Plugin dashboard cards require id, label, and url.")
        # Unknown keys are rejected rather than ignored: a typo'd optional key
        # would otherwise silently render nothing at all.
        unknown = card.keys() - CARD_REQUIRED_KEYS - CARD_OPTIONAL_KEYS
        if unknown:
            raise ImproperlyConfigured(f"Unknown dashboard card keys: {', '.join(sorted(unknown))}.")
        if isinstance(card["label"], Counted) == ("value" in card):
            raise ImproperlyConfigured(
                f"A dashboard card gives a value in words, or a Counted as its label and no value; got {card!r}."
            )
        value = card.get("value", "")
        if not isinstance(value, str) or value.replace(",", "").isdigit():
            raise ImproperlyConfigured(f"A dashboard card's count is a Counted as its label; got {value!r}.")
        status = card.get("status")
        if status is not None and status not in Tone:
            raise ImproperlyConfigured(f"Dashboard card status must be one of {', '.join(Tone)}; got {status!r}.")
        trend = card.get("trend")
        if trend is not None and trend not in CARD_TREND_VALUES:
            raise ImproperlyConfigured(
                f"Dashboard card trend must be one of {', '.join(sorted(CARD_TREND_VALUES))}; got {trend!r}."
            )


# The tones someone has to act on, most urgent first: a neutral item is
# context, never a call to action, and is left out of attention entirely.
ATTENTION_ORDER = tuple(tone for tone in Tone if tone.acted_on)


def gather_attention(
    sources: Iterable[tuple[str, str, Callable[[], Iterable[Any]] | None, tuple[str, ...]]],
) -> tuple[dict[str, Any], ...]:
    """Collect, validate, attribute and order attention items from any sources.

    One implementation for the host's sections and every extension, so no
    surface drifts on what "urgent" means. ``sources`` is ``(id, label,
    provider, read_capabilities)`` per contributor.
    """

    gathered: list[dict[str, Any]] = []
    for source_id, label, provider, read_capabilities in sources:
        if provider is None:
            continue
        for item in source_attention(source_id, label, provider, read_capabilities):
            # Only what someone can act on now: never context, and never what
            # waits on a fix that does not exist yet.
            if not item.severity.acted_on or item.awaiting_fix:
                continue
            gathered.append({"finding": item, "source": label, "source_id": source_id})
    return ordered_attention(gathered)


def ordered_attention(entries: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Gathered entries in the one order every surface shows them: by
    severity, what needs doing before what only reports, what HQ does in one
    press (``Finding.one_press``) before what a person does elsewhere, the
    longest-standing first, then by source and title."""

    def order(entry: dict[str, Any]) -> tuple[Any, ...]:
        item = entry["finding"]
        since = item.since
        return (
            item.severity.rank,
            item.notice is not None,
            not item.one_press,
            since is None,
            since.timestamp() if since is not None else 0.0,
            entry["source"],
            item.title,
        )

    return tuple(sorted(entries, key=order))


def plugin_attention_items() -> tuple[dict[str, Any], ...]:
    """What every installed extension believes needs a decision now.

    Extensions only: the SDK surface a composing extension renders its siblings
    with; HQ's own dashboard calls ``application.domains.domain_attention_items``.
    No exclusions, since the queue is the one place a `serious` item is sure to
    appear. A composer with per-domain panels groups these by `source_id` rather
    than asking a second channel.
    """

    # A notice reports something and asks for nothing, so it is not a
    # decision: it is in the queue's own section and nowhere under this name.
    return tuple(
        entry
        for entry in gather_attention(
            (plugin.id, plugin.name, integration.attention, plugin.mcp_read_capabilities)
            for plugin, integration in installed_integrations()
        )
        if not entry["finding"].notice
    )


def plugin_outbound_work() -> tuple[Any, ...]:
    return _provided("outbound")


def plugin_sequences() -> tuple[Any, ...]:
    return _provided("sequences")


def plugin_capability_specs() -> tuple[Any, ...]:
    return _provided("capabilities")


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
    }[kind]
    return frozenset(capability for plugin in installed_plugins() for capability in getattr(plugin, attribute))


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
