"""One registry of every domain HQ composes: host sections and extensions alike.

This module is the single declaration, for HQ's own sections as an extension's
manifest is for an extension. A domain states what it is once; nav,
and in turn every surface that composes domains, is derived from that. Nothing
downstream keeps its own list of what exists.

One thing is deliberately not unified. A ``PluginManifest`` also carries
*distribution* facts: wheel, admission policy, source workflow, URL mount,
because it crosses a trust boundary. A host section crosses none and is never
admitted. Runtime behavior is unified: host sections and extensions both carry
one typed, lazy ``PluginIntegration``. The registry normalises both into one
``Domain`` view so composing surfaces cannot tell, or care, which is which.

This repo is public. Host descriptors therefore never name an extension: group
labels arrive from installed manifests at runtime, and ``test_domains``
enforces that nothing here hardcodes one.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any, Callable, NamedTuple

from django.urls import URLResolver, include, path
from django.utils.module_loading import import_string

from .plugins import (
    NavigationItem,
    PluginIntegration,
    gather_attention,
    gather_cards,
    installed_integrations,
)
from .projection import read_once

# Order bands. Below HOST_ORDER_FLOOR is reserved for extension-supplied
# domains, so an installed extension leads the bar ahead of the host's own
# sections: the surfaces an operator opens daily are the ones a private
# extension provides, and the host's registries sit behind them. The host does
# not know which extensions exist, only that they sort first.
HOST_ORDER_FLOOR = 100
# Machinery, not work. Anything an operator consults only when something has
# already gone wrong sorts after every section that holds actual work,
# including sections added later by an extension.
HOST_ORDER_MACHINERY = 900


class Mount(NamedTuple):
    """Where a domain's URL configuration hangs off the root."""

    prefix: str
    urlconf: str


@dataclass(frozen=True)
class Records:
    """A plain record domain: one model created, changed and deleted by command.

    Declared once, it yields the domain's create, update and delete
    capabilities, its write and delete permissions, deletion itself and its
    count on the health reading. Anything else the domain can be asked to do is
    a command declared through ``integration.capabilities``.

    References are ``module:attribute`` strings so the declaration stays
    importable from settings, before any model is.
    """

    # Names the commands: "expense" gives expense.create, expense.delete.
    noun: str
    # The readable resource the commands act on. Also names the permissions:
    # "expenses" gives write_expenses and delete_expenses.
    resource: str
    model: str
    # A key of ``integration_specs.TARGET_KINDS``, and the model field it names.
    target: str
    lookup: str
    command: str
    # Creates when given no target, updates when given one.
    save: str
    upsert: str = ""
    create: bool = True
    # What a person calls one record, where the noun reads wrong.
    title: str = ""
    # Given the record about to go, returns what to run once the delete commits.
    on_delete: str = ""
    # The records any reader may count; every row when empty.
    visible: str = ""
    # (verb, summary, label) where the derived wording would say less than it should.
    wording: tuple[tuple[str, str, str], ...] = ()

    @property
    def write(self) -> str:
        return f"write_{self.resource}"

    @property
    def delete(self) -> str:
        return f"delete_{self.resource}"


@dataclass(frozen=True)
class DomainDescriptor:
    """A host section's whole declaration.

    ``id`` is stable and dotted so it can key attribution on surfaces that
    compose host and extension domains together, matching the shape a
    ``PluginManifest`` id already has. ``apps`` reach ``INSTALLED_APPS`` and
    ``mounts`` the root URL configuration; neither file lists a domain itself.
    """

    id: str
    label: str
    navigation: tuple[NavigationItem, ...] = ()
    integration: PluginIntegration = PluginIntegration()
    apps: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    records: Records | None = None


@dataclass(frozen=True)
class Domain:
    """A descriptor or a manifest, seen through one lens.

    ``origin`` exists for diagnostics and tests, not for behaviour. A surface
    that renders domains differently depending on who supplied them would
    reintroduce exactly the host/extension asymmetry this registry removes.
    """

    id: str
    label: str
    origin: str
    navigation: tuple[NavigationItem, ...]
    integration: PluginIntegration = PluginIntegration()
    records: Records | None = None

    @property
    def bar_order(self) -> int:
        """Where this domain sits in the nav, for surfaces that follow the bar.

        A domain with no nav entry sorts last rather than first: it has no
        claim on a position the operator has learned.
        """

        return min((item.order for item in self.navigation), default=HOST_ORDER_MACHINERY)


def load(reference: str) -> Any:
    """What a ``module:attribute`` reference in a declaration names."""

    module, separator, attribute = reference.partition(":")
    if not separator:
        raise ValueError(f"Reference {reference!r} must use module:attribute.")
    return import_string(f"{module}.{attribute}")


def _provider(reference: str) -> Callable[[], Any]:
    """Keep host declarations import-lazy while storing typed callables."""

    if ":" not in reference:
        raise ValueError(f"Provider {reference!r} must use module:attribute.")

    def provide() -> Any:
        return load(reference)()

    return provide


# ----- Host sections ---------------------------------------------------------
#
# Grouped by what the operator is doing, not by who owns the data: everything
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
        integration=PluginIntegration(
            connections=_provider("hq.platform.application.glance:connection_specs")
        ),
    ),
    DomainDescriptor(
        id="hq.calendar",
        label="Calendar",
        # Beside the dashboard until an extension places it in a group of its
        # own: the calendar is the host's, wherever it is listed.
        navigation=(NavigationItem("Calendar", "calendar:month", "calendar", 1, ""),),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.calendar_specs:capabilities"),
            resources=_provider("hq.platform.application.calendar_specs:resources"),
        ),
        apps=("hq.domains.calendars",),
        mounts=(Mount("calendar/", "hq.domains.calendars.urls"),),
    ),
    DomainDescriptor(
        id="hq.projects",
        label="Projects",
        navigation=(
            NavigationItem("Projects", "projects:list", "projects", 100, "Build"),
        ),
        # No attention provider, deliberately. "Active work with nothing
        # written about it yet" is a shape of the portfolio, not a decision:
        # only months of work clear it, so it does not belong in the queue.
        # The number shows on the projects card as "N need output".
        integration=PluginIntegration(
            resources=_provider("hq.domains.projects.specs:resources"),
            dashboard=_provider("hq.platform.application.sections:projects"),
            # What GitHub holds for a person: a deploy waiting on approval, a
            # failing default branch, a serious alert, a lapsing admission.
            # Decisions, not the portfolio's shape, which is why it is here.
            attention=_provider("hq.platform.application.github_posture:build_attention"),
        ),
        apps=("hq.domains.projects",),
        mounts=(Mount("projects/", "hq.domains.projects.urls"),),
        records=Records(
            noun="project",
            resource="projects",
            model="hq.domains.projects.models:Project",
            target="slug",
            lookup="slug",
            command="hq.platform.application.projects:ProjectCommand",
            save="hq.platform.application.projects:save_project",
            upsert="hq.platform.application.projects:upsert_project",
        ),
    ),
    DomainDescriptor(
        id="hq.watching",
        label="Watching",
        navigation=(NavigationItem("Watching", "watching", "", 102, "Build"),),
        integration=PluginIntegration(dashboard=_provider("hq.platform.application.sections:watching")),
    ),
    DomainDescriptor(
        id="hq.posture",
        label="Posture",
        navigation=(NavigationItem("Posture", "posture", "", 103, "Build"),),
    ),
    DomainDescriptor(
        id="hq.docs",
        label="Docs",
        navigation=(
            NavigationItem("Docs", "docs_index:list", "docs_index", 101, "Build"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.docs_index.specs:resources"),
            attention=_provider("hq.platform.application.attention:documentation"),
            dashboard=_provider("hq.platform.application.sections:documentation"),
        ),
        apps=("hq.domains.docs_index",),
        mounts=(Mount("docs/", "hq.domains.docs_index.urls"),),
        records=Records(
            noun="documentation",
            resource="documentation",
            model="hq.domains.docs_index.models:DocumentationRecord",
            target="doc_id",
            lookup="doc_id",
            command="hq.platform.application.documentation:DocumentationCommand",
            save="hq.platform.application.documentation:save_documentation",
            title="document",
            visible="hq.platform.application.sensitivity:safe_records",
        ),
    ),
    DomainDescriptor(
        # The id `hq.content` need not match the label. It is the registry's
        # stable key (what attribution is keyed on); the label is only what
        # an operator reads.
        id="hq.content",
        label="Writeups",
        # The published work, and the section this group exists for. Its
        # queues and card are declared here rather than on the sibling below
        # because they count the whole registry, both halves, and a queue
        # declared twice would report its backlog twice.
        navigation=(
            NavigationItem("Writeups", "content:writeups", "content", 110, "Web"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.content.specs:resources"),
            attention=_provider("hq.platform.application.attention:content"),
            dashboard=_provider("hq.platform.application.sections:content"),
        ),
        apps=("hq.domains.content",),
        mounts=(Mount("content/", "hq.domains.content.urls"),),
        records=Records(
            noun="content",
            resource="content",
            model="hq.domains.content.models:ContentItem",
            target="slug",
            lookup="slug",
            command="hq.platform.application.content:ContentCommand",
            save="hq.platform.application.content:save_content",
            title="content item",
        ),
    ),
    DomainDescriptor(
        id="hq.pages",
        label="Pages",
        # The structural pages: the ones that make the site navigable rather
        # than worth visiting. Same registry, same table, different half.
        navigation=(
            NavigationItem("Pages", "content:pages", "content", 111, "Web"),
        ),
    ),
    DomainDescriptor(
        id="hq.contacts",
        label="Contacts",
        navigation=(
            NavigationItem("Contacts", "contacts:list", "contacts", 112, "Web"),
        ),
        integration=PluginIntegration(
            capabilities=_provider("hq.domains.contacts.specs:capabilities"),
            resources=_provider("hq.domains.contacts.specs:resources"),
            attention=_provider("hq.platform.application.attention:contacts"),
            connections=_provider("hq.domains.contacts.d1:connection_specs"),
        ),
        apps=("hq.domains.contacts",),
        mounts=(Mount("contacts/", "hq.domains.contacts.urls"),),
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
            NavigationItem("Domains", "zones:index", "zones", 113, "Web"),
        ),
        mounts=(Mount("domains/", "hq.domains.control_plane.zone_urls"),),
    ),
    DomainDescriptor(
        id="hq.analytics",
        label="Analytics",
        # Last in Web, because it is the section that reports on the others.
        # Reading it is what you do after publishing, not before.
        navigation=(
            NavigationItem("Analytics", "analytics:overview", "analytics", 114, "Web"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.analytics.specs:resources"),
        ),
        apps=("hq.domains.analytics",),
        mounts=(Mount("analytics/", "hq.domains.analytics.urls"),),
    ),
    DomainDescriptor(
        id="hq.expenses",
        label="Expenses",
        navigation=(
            NavigationItem("Expenses", "expenses:list", "expenses", 120, "Business"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.expenses.specs:resources"),
            attention=_provider("hq.platform.application.attention:expenses"),
            dashboard=_provider("hq.platform.application.sections:expenses"),
        ),
        apps=("hq.domains.expenses",),
        mounts=(Mount("expenses/", "hq.domains.expenses.urls"),),
        records=Records(
            noun="expense",
            resource="expenses",
            model="hq.domains.expenses.models:Expense",
            target="integer",
            lookup="pk",
            command="hq.platform.application.expenses:ExpenseCommand",
            save="hq.platform.application.expenses:save_expense",
            wording=(("create", "Create an HQ expense.", "Record expense"),),
        ),
    ),
    DomainDescriptor(
        id="hq.receipts",
        label="Receipts",
        navigation=(
            NavigationItem("Receipts", "receipts:list", "receipts", 121, "Business"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.receipts.specs:resources"),
            attention=_provider("hq.platform.application.attention:receipts"),
        ),
        apps=("hq.domains.receipts",),
        mounts=(Mount("receipts/", "hq.domains.receipts.urls"),),
        # Receipts arrive as uploads, so there is no create command.
        records=Records(
            noun="receipt",
            resource="receipts",
            model="hq.domains.receipts.models:Receipt",
            target="integer",
            lookup="pk",
            command="hq.platform.application.receipts:ReceiptMetadataCommand",
            save="hq.platform.application.receipts:update_receipt",
            create=False,
            on_delete="hq.platform.application.receipts:file_cleanup",
            wording=(
                ("update", "Update receipt metadata and relationships (never file bytes).", ""),
                ("delete", "Delete a confirmed receipt and its private file.", ""),
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.assets",
        label="Assets",
        navigation=(
            NavigationItem("Assets", "assets:list", "assets", 122, "Business"),
        ),
        integration=PluginIntegration(
            resources=_provider("hq.domains.assets.specs:resources"),
            attention=_provider("hq.platform.application.attention:assets"),
        ),
        apps=("hq.domains.assets",),
        mounts=(Mount("assets/", "hq.domains.assets.urls"),),
        records=Records(
            noun="asset",
            resource="assets",
            model="hq.domains.assets.models:Asset",
            target="slug",
            lookup="slug",
            command="hq.platform.application.assets:AssetCommand",
            save="hq.platform.application.assets:save_asset",
            upsert="hq.platform.application.assets:upsert_asset",
        ),
    ),
    DomainDescriptor(
        id="hq.reports",
        label="Reports",
        navigation=(
            NavigationItem("Reports", "reports:dashboard", "reports", 123, "Business"),
        ),
        apps=("hq.domains.reports",),
        mounts=(Mount("reports/", "hq.domains.reports.urls"),),
    ),
    DomainDescriptor(
        id="hq.findings",
        label="Findings",
        navigation=(
            NavigationItem(
                "Findings", "control_plane:findings", "control_plane", 128,
                "Infrastructure",
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.topology",
        label="Topology",
        # The relational map leads the Infrastructure group because it is the
        # entry point that explains how every narrower workspace fits together.
        navigation=(
            NavigationItem(
                "Topology", "control_plane:topology", "control_plane", 129,
                "Infrastructure",
            ),
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
        integration=PluginIntegration(
            attention=_provider("hq.platform.application.attention:services"),
        ),
    ),
    DomainDescriptor(
        id="hq.estate",
        label="Estate",
        # No page of its own. Its card is machines, services, domains and
        # connections at once, each figure linking to the page that lists it.
        integration=PluginIntegration(
            attention=_provider("hq.platform.application.estate:attention"),
            dashboard=_provider("hq.platform.application.estate:cards"),
            overview=_provider("hq.platform.application.estate:overview"),
            calendars=_provider("hq.platform.application.calendar_sources:estate_sources"),
        ),
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
        integration=PluginIntegration(
            attention=_provider("hq.platform.application.attention:infrastructure")
        ),
        apps=("hq.domains.control_plane",),
        mounts=(Mount("infrastructure/", "hq.domains.control_plane.urls"),),
    ),
    DomainDescriptor(
        id="hq.tools",
        label="Tools",
        # One entry, however many tools. Every other Infrastructure section
        # reads something HQ holds; these read what the world holds, which is
        # the question an operator has while looking at the rest. Tools are
        # tabs inside this rather than siblings beside it, so the twentieth
        # costs no more of the bar than the first.
        navigation=(
            NavigationItem(
                "Tools", "control_plane:tools", "control_plane", 133,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            connections=_provider("hq.domains.control_plane.dns_lookup:connection_specs")
        ),
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
        id="hq.containers",
        label="Containers",
        # Beside machines, because a container is what a machine is for; the
        # question here is whether what runs on them is current and safe.
        navigation=(
            NavigationItem(
                "Containers", "control_plane:containers", "control_plane", 132,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(attention=_provider("hq.platform.application.containers:attention")),
    ),
    DomainDescriptor(
        id="hq.tailnet",
        label="Tailnet",
        # After machines, because it is about the network they are all on
        # rather than about any one of them, and the answers it gives are
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
        # at all": the question underneath every other page here, and the one
        # asked least often, because the answer only changes when a credential
        # does.
        navigation=(
            NavigationItem(
                "Connections", "control_plane:connections", "control_plane", 133,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            connections=_provider("hq.platform.application.connections:connection_specs")
        ),
    ),
    DomainDescriptor(
        id="hq.jobs",
        label="Jobs",
        navigation=(
            NavigationItem("Jobs", "jobs:list", "jobs", 132, "Infrastructure"),
        ),
        apps=("hq.domains.jobs",),
        mounts=(Mount("jobs/", "hq.domains.jobs.urls"),),
    ),
    DomainDescriptor(
        id="hq.audit",
        label="Audit",
        navigation=(
            NavigationItem(
                "Audit", "core:audit_list", "core", HOST_ORDER_MACHINERY, "System"
            ),
        ),
        integration=PluginIntegration(
            attention=_provider("hq.platform.application.attention:waiting_for_approval")
        ),
        mounts=(Mount("audit/", "hq.platform.core.urls"),),
    ),
    DomainDescriptor(
        id="hq.history",
        label="History",
        # No page of its own: what happened is read on the calendar, a day at
        # a time, and in full on the audit log.
        integration=PluginIntegration(
            calendars=_provider("hq.platform.application.calendar_sources:history_sources")
        ),
    ),
    DomainDescriptor(
        id="hq.agents",
        label="Agents",
        navigation=(
            NavigationItem("Agents", "agent_policy", "", HOST_ORDER_MACHINERY + 1, "System"),
        ),
    ),
    DomainDescriptor(
        id="hq.api",
        label="API",
        # Beside Agents: the machine API is what an agent's token reaches.
        navigation=(
            NavigationItem(
                "API", "api_reference:reference", "api_reference",
                HOST_ORDER_MACHINERY + 2, "System",
            ),
        ),
        mounts=(Mount("api/docs/", "hq.platform.api.web_urls"),),
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
            integration=descriptor.integration,
            records=descriptor.records,
        )
        for descriptor in HOST_DOMAINS
    )


def host_apps() -> list[str]:
    """The Django apps the host's domains own, for ``INSTALLED_APPS``."""

    return list(dict.fromkeys(app for descriptor in HOST_DOMAINS for app in descriptor.apps))


def host_urlpatterns() -> list[URLResolver]:
    """Each domain's URL configuration, mounted where it declares."""

    return [
        path(mount.prefix, include(mount.urlconf))
        for descriptor in HOST_DOMAINS
        for mount in descriptor.mounts
    ]


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
        )
        for plugin, integration in installed_integrations()
    )


def all_domains() -> tuple[Domain, ...]:
    """Every domain HQ composes, host and extension, in id order.

    Id order rather than nav order: this is the registry, and a caller wanting
    presentation order asks ``domain_navigation`` for it.
    """

    return tuple(
        sorted((*host_domains(), *extension_domains()), key=lambda domain: domain.id)
    )


def host_specs(kind: str) -> tuple[Any, ...]:
    """What the host's domains declare of one kind: connections, capabilities, resources.

    Extensions cross a separate admission boundary and continue through the
    plugin registry. Host domains use the same late-bound provider shape, so a
    command, a readable resource or a gateway is registered once beside its
    domain instead of being copied into a central list that every domain grows.
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
    placed = {
        item.route
        for domain in domains
        if domain.origin == "extension"
        for item in domain.navigation
    }
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
        items.append(item)
    return tuple(items)


def domain_attention_items() -> tuple[dict[str, Any], ...]:
    """Everything, anywhere in HQ, that needs a decision: most urgent first.

    The composed queue, host sections and extensions alike. Each entry carries
    its source so a surface can attribute the item without the domain restating
    its own name, and each Insight carries its own url.

    A domain reports only what is actually outstanding: an item with nothing to
    do is simply not emitted, rather than emitted as a zero for a reader to
    filter. ``neutral`` and ``good`` are context, not a call to action, and are
    excluded here the same way they are for extensions.
    """

    return gather_attention(
        (domain.id, domain.label, domain.integration.attention)
        for domain in all_domains()
    )


def domain_dashboard_cards() -> tuple[dict[str, Any], ...]:
    """Every domain's headline reading, in the order the nav presents them.

    One row of cards, host sections and extensions alike, in nav order.

    Ordered by nav position so the row reads in the same sequence as the
    sections above it. A domain reporting nothing contributes nothing, which is
    what keeps a section with no data from taking a tile on the page an
    operator reads every day.
    """

    return tuple(
        card for section in domain_dashboard_sections() for card in section["cards"]
    )


def domain_dashboard_sections() -> tuple[dict[str, Any], ...]:
    """Keep each contributor's metrics together without naming its domain."""

    return read_once("domains.dashboard_sections", _dashboard_sections)


def _given(cards: tuple[dict[str, Any], ...]) -> Callable[[], tuple[dict[str, Any], ...]]:
    return lambda: cards


def _dashboard_sections() -> tuple[dict[str, Any], ...]:
    sections: list[dict[str, Any]] = []
    for domain in sorted(all_domains(), key=lambda domain: domain.bar_order):
        if domain.integration.dashboard is None:
            continue
        cards = tuple(domain.integration.dashboard())
        if cards:
            sections.append({"id": domain.id, "label": domain.label, "cards": cards})
    # One validation still catches collisions across contributors.
    gather_cards((section["id"], _given(section["cards"])) for section in sections)
    return tuple(sections)
