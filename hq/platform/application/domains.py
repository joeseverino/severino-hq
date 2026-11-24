"""One registry of every domain HQ composes: host sections and extensions alike.

A host section is declared in ``host_sections`` as an extension is by its
manifest, and nav and every surface that composes domains derive from this
registry; nothing downstream keeps a
list of its own. Group labels arrive from installed manifests at runtime, so
nothing here names an extension (``test_domains`` holds it).
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache, partial
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from . import host_sections
from .composed_providers import DERIVED_PROVIDERS, answered_by
from .derivations import derivation
from .derived_inputs import DASHBOARD_READS, QUEUE_READS, composed_variant, estate_variant
from .host_sections import HOST_ORDER_MACHINERY, Viewed
from .plugins import (
    NavigationGroup,
    NavigationItem,
    PluginIntegration,
    gather_attention,
    gather_cards,
    installed_integrations,
    ordered_attention,
    source_cards,
)
from .record_declaration import Records
from .routes import reverse, routed
lazy from . import attention
lazy from .security import Capability, Principal, could_hold


# Runtime behavior is one typed, lazy ``PluginIntegration`` for both. The
# distribution facts (wheel, admission policy, source workflow, URL mount) stay
# the ``PluginManifest``'s alone, since an extension crosses a trust boundary
# and a host section crosses none and is never admitted.
@dataclass(frozen=True, slots=True)
class Domain:
    """A descriptor or a manifest, seen through one lens.

    ``origin`` exists for diagnostics and tests, not for behavior. A surface
    that renders domains differently depending on who supplied them would
    reintroduce exactly the host/extension asymmetry this registry removes.
    """

    id: str
    label: str
    origin: str
    navigation: tuple[NavigationItem, ...]
    integration: PluginIntegration = PluginIntegration()
    records: Records | None = None
    # A host section's ``DomainDescriptor.description``, or an extension manifest's.
    description: str = ""
    # What each nav group an extension's manifest adds is for; the host's own
    # say theirs in ``NAVIGATION_GROUPS``.
    groups: tuple[NavigationGroup, ...] = ()
    # What a reader of this domain holds: an extension's manifest's reads
    # (``mcp_read_capabilities``). Empty for a host section, whose items each
    # carry their own gate.
    read_capabilities: tuple[str, ...] = ()

    @property
    def bar_order(self) -> int:
        """Where this domain sits in the nav, for surfaces that follow the bar.

        A domain with no nav entry sorts last rather than first: it has no
        claim on a position the operator has learned.
        """

        return min((item.order for item in self.navigation), default=HOST_ORDER_MACHINERY)


def provided(provider: Callable[[], Any], principal: Principal) -> Any:
    """A dashboard or overview provider's answer, for ``principal`` when it reads one."""

    return provider.viewed(principal) if isinstance(provider, Viewed) else provider()


def reader_variant(principal: Principal) -> tuple[Any, ...]:
    """What a composition read for ``principal`` depends on: what they may read.

    The capabilities ``principal.permits`` honors, so readers who may read the
    same things share one answer whoever and wherever they are.
    """

    held = sorted(
        str(item.value if isinstance(item, Capability) else item)
        for item in principal.capabilities
        if could_hold(principal.interface, (item,))
    )
    return (tuple(held), *composed_variant())


@cache
def host_domains() -> tuple[Domain, ...]:
    return tuple(
        Domain(
            id=descriptor.id,
            label=descriptor.label,
            origin="host",
            navigation=descriptor.navigation,
            integration=descriptor.integration,
            records=descriptor.records,
            description=descriptor.description,
        )
        for descriptor in host_sections.HOST_DOMAINS
    )


def host_apps() -> list[str]:
    """The Django apps the host's domains own, for ``INSTALLED_APPS``."""

    return list(dict.fromkeys(app for descriptor in host_sections.HOST_DOMAINS for app in descriptor.apps))


def host_records() -> tuple[Records, ...]:
    return tuple(domain.records for domain in host_domains() if domain.records)


def records_of(resource: str) -> Records:
    """The record declaration for one resource; a typo fails at first use."""

    for records in host_records():
        if records.resource == resource:
            return records
    raise LookupError(f"No host domain declares records for {resource!r}.")


def extension_domains() -> tuple[Domain, ...]:
    """Installed extensions, seen as domains.

    Not cached here: ``installed_plugins`` already is, and caching the derived
    view as well would mean two places to invalidate.
    """

    return tuple(
        Domain(
            id=plugin.id,
            label=plugin.name,
            origin="extension",
            navigation=plugin.navigation,
            integration=integration,
            description=plugin.description,
            groups=plugin.navigation_groups,
            read_capabilities=plugin.mcp_read_capabilities,
        )
        for plugin, integration in installed_integrations()
    )


def all_domains() -> tuple[Domain, ...]:
    """Every domain HQ composes, host and extension, in id order.

    Id order rather than nav order: this is the registry, and a caller wanting
    presentation order asks ``domain_navigation`` for it.
    """

    domains = tuple(sorted((*host_domains(), *extension_domains()), key=lambda domain: domain.id))
    # An id keys attribution on every surface that composes the two.
    shared = sorted({a.id for a, b in itertools.pairwise(domains) if a.id == b.id})
    if shared:
        raise ImproperlyConfigured(f"Two domains declare the id {', '.join(shared)}.")
    return domains


def composition() -> tuple[tuple[str, str, str], ...]:
    """Which function answers for each extension's part of a shared composition.

    A derivation that composes every domain varies by this: the same rows
    composed by other providers are another answer.
    """

    return tuple(
        (domain.id, field, answered_by(provider))
        for domain in all_domains()
        if domain.origin != "host"
        for field in DERIVED_PROVIDERS
        if (provider := getattr(domain.integration, field)) is not None
    )


def host_specs(kind: str) -> tuple[Any, ...]:
    """What the host's domains declare of one kind: connections, capabilities, resources, outbound work.

    Each is declared once beside its domain, in the provider shape an
    extension's integration has; no central list names any of them.
    """

    return tuple(
        spec
        for domain in host_domains()
        if (provider := getattr(domain.integration, kind)) is not None
        for spec in provider()
    )


def domain_navigation() -> tuple[NavigationItem, ...]:
    """Every domain's nav items, in presentation order.

    Sorted by ``(order, label)`` so a tie between a host section and an
    extension resolves the same way every render rather than by registry
    ordering, which would make the bar depend on which extensions are
    installed.
    """

    domains = all_domains()
    # An extension may place a host page in its own group by naming its route.
    # Where one does, the host's own entry for that page steps aside; where
    # none does, the page stays where the host puts it.
    placed = {item.route for domain in domains if domain.origin == "extension" for item in domain.navigation}
    items: list[NavigationItem] = []
    seen: set[str] = set()
    for item in sorted(
        (
            item
            for domain in domains
            for item in domain.navigation
            if not (domain.origin == "host" and item.route in placed)
        ),
        key=lambda item: (item.order, item.label),
    ):
        # Two extensions placing the one page: the first in order keeps it.
        if item.route in seen:
            continue
        seen.add(item.route)
        items.append(replace(item, label=page_title(item.route) or item.label))
    return tuple(items)


@cache
def page_title(route: str) -> str:
    """What the page at ``route`` is titled, when its view says so before it
    draws: an entry is called what its page is, and names itself only for a
    page titled by what it shows (a zone, the greeting)."""

    views = (getattr(view.pattern.callback, "view_class", None) for view in routed() if view.name == route)
    return getattr(next(views, None), "page_title", "")


def domain_attention_items() -> tuple[dict[str, Any], ...]:
    """Everything, anywhere in HQ, that needs a decision: most urgent first.

    The composed queue, host sections and extensions alike, each entry
    attributed to its source and carrying its own url (``gather_attention``).
    A domain emits only what is outstanding, never a zero for a reader to
    filter.
    """

    # Each extension's provider is a derivation of its own (``plugins``), so
    # this gathers stored answers and orders them.
    extensions = gather_attention(
        (domain.id, domain.label, domain.integration.attention, domain.read_capabilities)
        for domain in all_domains()
        if domain.origin != "host"
    )
    # One order for both: an extension's entries are merged in by the same key.
    return ordered_attention((*host_attention(), *extensions))


def attention_urls() -> dict[str, str]:
    """Each domain that declares one, by id: the page its attention items are decided on together."""

    return {
        domain.id: domain.integration.attention_url()
        for domain in all_domains()
        if domain.integration.attention_url is not None
    }


def resources_of(domain: str) -> tuple[str, ...]:
    """The resources a host section declares, by name: what a reader of its records reads."""

    return tuple(
        spec.name
        for found in host_domains()
        if found.id == domain and found.integration.resources is not None
        for spec in found.integration.resources()
    )


def queue_sources() -> tuple[str, ...]:
    """Every domain that raises items on the queue, by id."""

    raising = attention.raising_domains()
    return tuple(
        domain.id for domain in all_domains() if domain.id in raising or domain.integration.attention is not None
    )


def _host_queue(domain: str) -> Callable[[], tuple[Any, ...]]:
    if domain == attention.APPROVALS:
        return attention.waiting_for_approval
    return partial(attention.raised, domain)


@derivation("attention.queue", reads=QUEUE_READS, vary=estate_variant)
def host_attention() -> tuple[dict[str, Any], ...]:
    """What the host's own sections report, derived once per change of the
    estate: their finding rules' findings, and the held requests."""

    raising = attention.raising_domains()
    return gather_attention(
        (domain.id, domain.label, _host_queue(domain.id), domain.read_capabilities)
        for domain in host_domains()
        if domain.id in raising
    )


def domain_dashboard_cards(principal: Principal) -> tuple[dict[str, Any], ...]:
    """Every domain's headline reading as ``principal`` may read it, host
    sections and extensions alike, in nav order so the row reads in the same
    sequence as the sections above it. A domain reporting nothing takes no
    tile on the page an operator reads every day.
    """

    return tuple(card for section in domain_dashboard_sections(principal) for card in section["cards"])


def _given(cards: tuple[dict[str, Any], ...]) -> Callable[[], tuple[dict[str, Any], ...]]:
    return lambda: cards


@derivation("dashboard.sections", reads=DASHBOARD_READS, vary=reader_variant)
def domain_dashboard_sections(principal: Principal) -> tuple[dict[str, Any], ...]:
    """Keep each contributor's metrics together without naming its domain.

    Derived once per change of what the host's sections read, for each set of
    capabilities a reader holds; each extension's cards are a derivation of
    their own.
    """

    sections: list[dict[str, Any]] = []
    for domain in sorted(all_domains(), key=lambda domain: domain.bar_order):
        if domain.integration.dashboard is None:
            continue
        dashboard = domain.integration.dashboard
        home = reverse(domain.navigation[0].route) if domain.navigation else ""
        cards = source_cards(domain.id, domain.label, home, partial(provided, dashboard, principal))
        if cards:
            sections.append({"id": domain.id, "label": domain.label, "cards": cards})
    # One validation still catches collisions across contributors.
    gather_cards((section["id"], _given(section["cards"])) for section in sections)
    return tuple(sections)
