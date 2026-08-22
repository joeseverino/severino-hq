"""One registry of every domain HQ composes -- host sections and extensions alike.

HQ's own sections were previously declared three times over: a nav tuple in
``core.context_processors``, a hand-built work-queue list in
``application.dashboard``, and a code-to-URL dict in ``core.views`` that existed
only to rejoin the other two. Adding a section meant editing all three, and a
section that appeared in one but not the others was a silent hole rather than a
failure. Meanwhile an *extension* declared the same facts once, in its manifest,
and got every surface for free.

This module is the single declaration. A domain states what it is once; nav,
and in turn every surface that composes domains, is derived from that. Nothing
downstream keeps its own list of what exists.

One thing is deliberately not unified. A ``PluginManifest`` also carries
*distribution* facts -- wheel, admission policy, source workflow, URL mount --
because it crosses a trust boundary. A host section crosses none and is never
admitted. Everything else is the same contract, down to providers being
late-bound ``module:attribute`` strings rather than callables: it keeps this
module free of model imports, which matters because the nav resolves through it
on every request, and it means one resolver serves both. The registry
normalises both into one ``Domain`` view so composing surfaces cannot tell, or
care, which is which.

This repo is public. Host descriptors therefore never name an extension: group
labels arrive from installed manifests at runtime, and ``test_domains``
enforces that nothing here hardcodes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any, Iterable

from .plugins import (
    NavigationItem,
    gather_attention,
    gather_cards,
    installed_plugins,
)

# Order bands. Below HOST_ORDER_FLOOR is reserved for extension-supplied
# domains, so an installed extension leads the bar ahead of the host's own
# sections -- the surfaces an operator opens daily are the ones a private
# extension provides, and the host's registries sit behind them. The host does
# not know which extensions exist, only that they sort first.
HOST_ORDER_FLOOR = 100
# Machinery, not work. Anything an operator consults only when something has
# already gone wrong sorts after every section that holds actual work,
# including sections added later by an extension.
HOST_ORDER_MACHINERY = 900


@dataclass(frozen=True)
class DomainDescriptor:
    """A host section's whole declaration.

    ``id`` is stable and dotted so it can key attribution on surfaces that
    compose host and extension domains together, matching the shape a
    ``PluginManifest`` id already has.
    """

    id: str
    label: str
    navigation: tuple[NavigationItem, ...] = ()
    # ``module:attribute`` returning the Insights this section believes need a
    # decision now. Each Insight carries its own url, which is why no surface
    # downstream needs a code-to-URL table to render the queue.
    attention_provider: str = ""
    # ``module:attribute`` returning this section's headline dashboard cards.
    # A section with nothing worth reporting returns none and simply does not
    # appear, which is how a dormant section stays out of the way.
    cards_provider: str = ""


@dataclass(frozen=True)
class Domain:
    """A descriptor or a manifest, seen through one lens.

    ``origin`` exists for diagnostics and tests -- not for behaviour. A surface
    that renders domains differently depending on who supplied them would
    reintroduce exactly the host/extension asymmetry this registry removes.
    """

    id: str
    label: str
    origin: str
    navigation: tuple[NavigationItem, ...]
    attention_provider: str = ""
    cards_provider: str = ""

    @property
    def bar_order(self) -> int:
        """Where this domain sits in the nav, for surfaces that follow the bar.

        A domain with no nav entry sorts last rather than first: it has no
        claim on a position the operator has learned.
        """

        return min((item.order for item in self.navigation), default=HOST_ORDER_MACHINERY)


# ----- Host sections ---------------------------------------------------------
#
# Grouped by what the operator is doing, not by who owns the data -- everything
# in HQ is the operator's, so ownership cannot discriminate. Build is what gets
# made; Web is the public site and what it publishes; Business is the company
# ledger; Infrastructure is declared state a controller reconciles; System is
# the machinery.

HOST_DOMAINS: tuple[DomainDescriptor, ...] = (
    DomainDescriptor(
        id="hq.dashboard",
        label="Dashboard",
        # The one inline entry: no group, so it renders as a bare link at the
        # head of the bar rather than a dropdown of one.
        navigation=(NavigationItem("Dashboard", "dashboard", "", 0, ""),),
    ),
    DomainDescriptor(
        id="hq.projects",
        label="Projects",
        navigation=(
            NavigationItem("Projects", "projects:list", "projects", 100, "Build"),
        ),
        attention_provider="application.attention:projects",
        cards_provider="application.sections:projects",
    ),
    DomainDescriptor(
        id="hq.docs",
        label="Docs",
        navigation=(
            NavigationItem("Docs", "docs_index:list", "docs_index", 101, "Build"),
        ),
        attention_provider="application.attention:documentation",
        cards_provider="application.sections:documentation",
    ),
    DomainDescriptor(
        id="hq.content",
        label="Content",
        navigation=(
            NavigationItem("Content", "content:list", "content", 110, "Web"),
        ),
        attention_provider="application.attention:content",
        cards_provider="application.sections:content",
    ),
    DomainDescriptor(
        id="hq.contacts",
        label="Contacts",
        navigation=(
            NavigationItem("Contacts", "contacts:list", "contacts", 111, "Web"),
        ),
        attention_provider="application.attention:contacts",
    ),
    DomainDescriptor(
        id="hq.zones",
        label="Domains",
        # Under Web, not Infrastructure. Web is the public site and what it
        # publishes, and a zone is exactly that. The declarations behind it are
        # infrastructure resources and stay listed there; this is where the
        # records themselves are read and changed, which is a different job done
        # on a different day.
        navigation=(
            NavigationItem("Domains", "zones:index", "zones", 112, "Web"),
        ),
    ),
    DomainDescriptor(
        id="hq.expenses",
        label="Expenses",
        navigation=(
            NavigationItem("Expenses", "expenses:list", "expenses", 120, "Business"),
        ),
        attention_provider="application.attention:expenses",
        cards_provider="application.sections:expenses",
    ),
    DomainDescriptor(
        id="hq.receipts",
        label="Receipts",
        navigation=(
            NavigationItem("Receipts", "receipts:list", "receipts", 121, "Business"),
        ),
        attention_provider="application.attention:receipts",
    ),
    DomainDescriptor(
        id="hq.assets",
        label="Assets",
        navigation=(
            NavigationItem("Assets", "assets:list", "assets", 122, "Business"),
        ),
        attention_provider="application.attention:assets",
    ),
    DomainDescriptor(
        id="hq.reports",
        label="Reports",
        navigation=(
            NavigationItem("Reports", "reports:dashboard", "reports", 123, "Business"),
        ),
    ),
    DomainDescriptor(
        id="hq.services",
        label="Services",
        # Ahead of Resources deliberately. A resource is a declaration a
        # controller reconciles; a service is the thing an operator was actually
        # thinking of when they opened this group. The registry stays one click
        # further in, where the answer is "which declaration is wrong".
        navigation=(
            NavigationItem(
                "Services", "control_plane:services", "control_plane", 130,
                "Infrastructure",
            ),
        ),
        attention_provider="application.attention:services",
        cards_provider="application.sections:services",
    ),
    DomainDescriptor(
        id="hq.infrastructure",
        label="Infrastructure",
        navigation=(
            NavigationItem(
                "Resources", "control_plane:list", "control_plane", 131,
                "Infrastructure",
            ),
        ),
        attention_provider="application.attention:infrastructure",
    ),
    DomainDescriptor(
        id="hq.machines",
        label="Machines",
        # Between the things HQ manages and the credentials it manages them
        # with, because that is what a machine is: the thing both halves are
        # about.
        navigation=(
            NavigationItem(
                "Machines", "control_plane:machines", "control_plane", 132,
                "Infrastructure",
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.tailnet",
        label="Tailnet",
        # After machines, because it is about the network they are all on
        # rather than about any one of them -- and the answers it gives are
        # only meaningful once you know which machine you are asking about.
        navigation=(
            NavigationItem(
                "Tailnet", "control_plane:tailnet", "control_plane", 133,
                "Infrastructure",
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.connections",
        label="Connections",
        # Last in the group, and deliberately so. It answers "what can HQ reach
        # at all" -- the question underneath every other page here, and the one
        # asked least often, because the answer only changes when a credential
        # does.
        navigation=(
            NavigationItem(
                "Connections", "control_plane:connections", "control_plane", 133,
                "Infrastructure",
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.jobs",
        label="Jobs",
        navigation=(
            NavigationItem("Jobs", "jobs:list", "jobs", 132, "Infrastructure"),
        ),
    ),
    DomainDescriptor(
        id="hq.audit",
        label="Audit",
        navigation=(
            NavigationItem(
                "Audit", "core:audit_list", "core", HOST_ORDER_MACHINERY, "System"
            ),
        ),
    ),
)


@cache
def host_domains() -> tuple[Domain, ...]:
    return tuple(
        Domain(
            id=descriptor.id,
            label=descriptor.label,
            origin="host",
            navigation=descriptor.navigation,
            attention_provider=descriptor.attention_provider,
            cards_provider=descriptor.cards_provider,
        )
        for descriptor in HOST_DOMAINS
    )


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
            attention_provider=plugin.attention_provider,
            cards_provider=plugin.dashboard_provider,
        )
        for plugin in installed_plugins()
    )


def all_domains() -> tuple[Domain, ...]:
    """Every domain HQ composes, host and extension, in id order.

    Id order rather than nav order: this is the registry, and a caller wanting
    presentation order asks ``domain_navigation`` for it.
    """

    return tuple(
        sorted((*host_domains(), *extension_domains()), key=lambda domain: domain.id)
    )


def domain_navigation() -> tuple[NavigationItem, ...]:
    """Every domain's nav items, in presentation order.

    Sorted by ``(order, label)`` so a tie between a host section and an
    extension resolves the same way every render rather than by registry
    ordering, which would make the bar depend on which extensions are
    installed.
    """

    items: Iterable[NavigationItem] = (
        item for domain in all_domains() for item in domain.navigation
    )
    return tuple(sorted(items, key=lambda item: (item.order, item.label)))


def domain_attention_items() -> tuple[dict[str, Any], ...]:
    """Everything, anywhere in HQ, that needs a decision -- most urgent first.

    The composed queue. Previously the host built its own list from eight
    hardcoded queries while extensions had a separate channel nothing on the
    dashboard read, so an extension could be on fire and the page titled "here
    is what needs doing" would say nothing about it.

    Each entry carries its source so a surface can attribute the item without
    the domain restating its own name, and each Insight carries its own url --
    which is what retired the code-to-URL table the dashboard used to keep.

    A domain reports only what is actually outstanding: an item with nothing to
    do is simply not emitted, rather than emitted as a zero for a reader to
    filter. ``neutral`` and ``good`` are context, not a call to action, and are
    excluded here the same way they are for extensions.
    """

    return gather_attention(
        (domain.id, domain.label, domain.attention_provider)
        for domain in all_domains()
    )


def domain_dashboard_cards() -> tuple[dict[str, Any], ...]:
    """Every domain's headline reading, in the order the nav presents them.

    One row of cards rather than two. The host's own figures were previously
    hand-written into the template as five fixed tiles -- which were not even
    links, while the extension cards beside them were -- so a new host section
    meant editing markup, and the row's order could not follow the bar.

    Ordered by nav position so the row reads in the same sequence as the
    sections above it. A domain reporting nothing contributes nothing, which is
    what keeps a section with no data from taking a tile on the page an
    operator reads every day.
    """

    return gather_cards(
        (domain.id, domain.cards_provider)
        for domain in sorted(all_domains(), key=lambda domain: domain.bar_order)
    )
