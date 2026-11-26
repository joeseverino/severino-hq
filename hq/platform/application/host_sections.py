"""The host's own sections, each declared once as an extension is by its manifest.

``domains`` composes them with the installed extensions; nothing else keeps a
list of them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .plugins import NavigationItem, PluginIntegration
from .record_declaration import Records
lazy from .security import Principal

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


@dataclass(frozen=True, slots=True)
class DomainDescriptor:
    """A host section's whole declaration.

    ``id`` is stable and dotted so it can key attribution on surfaces that
    compose host and extension domains together, matching the shape a
    ``PluginManifest`` id already has. ``apps`` reach ``INSTALLED_APPS`` and
    ``mounts`` the root URL configuration; neither file lists a domain itself.
    """

    id: str
    label: str
    # What the section is for, in a sentence a reader of HQ's machine contract
    # reads under its name (the API document's tag).
    description: str = ""
    navigation: tuple[NavigationItem, ...] = ()
    integration: PluginIntegration = PluginIntegration()
    apps: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    records: Records | None = None

    def __post_init__(self) -> None:
        # What a host section puts on the queue is its finding rules' findings
        # (``FindingRule.domain``), and the held requests are Approvals'.
        if self.integration.attention is not None:
            raise ImproperlyConfigured(f"{self.id} raises queue items as finding rules, not an attention provider.")
        # The band below the floor is the extensions', which this repository
        # cannot see; an ungrouped item renders inline and may lead.
        for item in self.navigation:
            if item.group and item.order < HOST_ORDER_FLOOR:
                raise ImproperlyConfigured(
                    f"{self.id}'s {item.label!r} is ordered {item.order}, in the extensions' band below "
                    f"{HOST_ORDER_FLOOR}."
                )


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


@dataclass(frozen=True, slots=True)
class Viewed:
    """A host provider whose answer depends on what its reader may read.

    Its function takes the reader as ``principal``. Asked with none, it
    refuses rather than answer for no one, so a figure the reader may not see
    is never run on their behalf.
    """

    reference: str

    def __call__(self) -> Any:
        raise ImproperlyConfigured(f"{self.reference} answers for a reader; ask it through provided().")

    def viewed(self, principal: Principal) -> Any:
        return load(self.reference)(principal=principal)


# ----- Host sections ---------------------------------------------------------
#
# Grouped by what the operator is doing, not by who owns the data: everything
# in HQ is the operator's, so ownership cannot discriminate. Each group's
# purpose is ``NAVIGATION_GROUPS``.

# What each nav group is for, by the name its items give it; a group an
# extension adds says so in its manifest (``PluginManifest.navigation_groups``).
NAVIGATION_GROUPS = {
    "Projects": "What gets made: the projects, their documentation and the repositories behind them.",
    "Web": "The public site and what it publishes, the messages it receives and how it is read.",
    "Business": "The company ledger: expenses, receipts, assets and the reports drawn from them.",
    "Infrastructure": "What HQ expects of the live systems and keeps them to, and the machines, services and connections "
    "they run on.",
    "System": "HQ's own machinery: its audit log, its agents, this API and HQ itself.",
}

HOST_DOMAINS: tuple[DomainDescriptor, ...] = (
    DomainDescriptor(
        id="hq.dashboard",
        label="Dashboard",
        description="The first page: what needs attention and how each part of HQ stands.",
        # The one inline entry: no group, so it renders as a bare link at the
        # head of the bar rather than a dropdown of one.
        navigation=(NavigationItem("Dashboard", "dashboard", "", 0, ""),),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.dashboard_specs:capabilities"),
            resources=_provider("hq.platform.application.dashboard_specs:resources"),
            connections=_provider("hq.platform.application.glance:connection_specs"),
        ),
    ),
    DomainDescriptor(
        id="hq.calendar",
        label="Calendar",
        description="Events on HQ's calendar and the dates every other domain adds, read as one agenda.",
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
        description="The projects being built, their repositories and what GitHub says of them.",
        navigation=(NavigationItem("Projects", "projects:list", "projects", 100, "Projects"),),
        # No finding rule for it, deliberately. "Active work with nothing
        # written about it yet" is a shape of the portfolio, not a decision:
        # only months of work clear it, so it does not belong in the queue.
        # The number shows on the projects card as "N need output".
        integration=PluginIntegration(
            capabilities=_provider("hq.domains.projects.specs:capabilities"),
            resources=_provider("hq.domains.projects.specs:resources"),
            dashboard=_provider("hq.platform.application.sections:projects"),
            outbound=_provider("hq.platform.application.projects:outbound_work"),
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
        ),
    ),
    DomainDescriptor(
        id="hq.watching",
        label="Starred repos",
        description="Repositories starred on GitHub, and what changed in them.",
        navigation=(NavigationItem("Starred repos", "watching", "", 102, "Projects"),),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.github_profile:capabilities"),
            dashboard=_provider("hq.platform.application.sections:watching"),
        ),
    ),
    DomainDescriptor(
        id="hq.posture",
        label="Repo rules",
        description="Each repository's settings, checked against the standard HQ holds them to.",
        navigation=(NavigationItem("Repo rules", "posture", "", 103, "Projects"),),
    ),
    DomainDescriptor(
        id="hq.docs",
        label="Docs",
        description="Pointers to the documentation of each system and project, kept by sensitivity.",
        navigation=(NavigationItem("Docs", "docs_index:list", "docs_index", 101, "Projects"),),
        integration=PluginIntegration(
            capabilities=_provider("hq.domains.docs_index.specs:capabilities"),
            resources=_provider("hq.domains.docs_index.specs:resources"),
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
        description="The writeups the public site publishes, and where each stands.",
        # The published work, and the section this group exists for. Its
        # queues and card are declared here rather than on the sibling below
        # because they count the whole registry, both halves, and a queue
        # declared twice would report its backlog twice.
        navigation=(NavigationItem("Writeups", "content:writeups", "content", 110, "Web"),),
        integration=PluginIntegration(
            resources=_provider("hq.domains.content.specs:resources"),
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
            variants=(
                ("create", "writeup_or_page", "Add a writeup or page"),
                ("create", "writeup", "Add a writeup"),
                ("create", "page", "Add a page"),
            ),
            # Its description is written in sentences.
        ),
    ),
    DomainDescriptor(
        id="hq.pages",
        label="Pages",
        description="The public site's structural pages.",
        # The structural pages: the ones that make the site navigable rather
        # than worth visiting. Same registry, same table, different half.
        navigation=(NavigationItem("Pages", "content:pages", "content", 111, "Web"),),
    ),
    DomainDescriptor(
        id="hq.contacts",
        label="Contacts",
        description="The messages sent through the public site's contact form, and their review.",
        navigation=(NavigationItem("Messages", "contacts:list", "contacts", 112, "Web"),),
        integration=PluginIntegration(
            capabilities=_provider("hq.domains.contacts.specs:capabilities"),
            resources=_provider("hq.domains.contacts.specs:resources"),
        ),
        apps=("hq.domains.contacts",),
        mounts=(Mount("contacts/", "hq.domains.contacts.urls"),),
    ),
    DomainDescriptor(
        id="hq.zones",
        label="Domains",
        description="The domains HQ manages and the records each holds.",
        # Under Web, not Infrastructure. Web is the public site and what it
        # publishes, and a zone is exactly that. The declarations behind it are
        # infrastructure resources and stay listed there; this is where the
        # records themselves are read and changed, which is a different job done
        # on a different day.
        navigation=(NavigationItem("Domains", "zones:index", "zones", 113, "Web"),),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.zones:capabilities"),
            resources=_provider("hq.platform.application.zones:resources"),
        ),
        mounts=(Mount("domains/", "hq.domains.control_plane.zone_urls"),),
    ),
    DomainDescriptor(
        id="hq.analytics",
        label="Analytics",
        description="What visitors asked the public site for, by each breakdown HQ records.",
        # Last in Web, because it is the section that reports on the others.
        # Reading it is what you do after publishing, not before.
        navigation=(NavigationItem("Analytics", "analytics:overview", "analytics", 114, "Web"),),
        integration=PluginIntegration(resources=_provider("hq.domains.analytics.specs:resources")),
        apps=("hq.domains.analytics",),
        mounts=(Mount("analytics/", "hq.domains.analytics.urls"),),
    ),
    DomainDescriptor(
        id="hq.expenses",
        label="Expenses",
        description="The business's expenses, and how much of each is for the business.",
        navigation=(NavigationItem("Expenses", "expenses:list", "expenses", 120, "Business"),),
        integration=PluginIntegration(
            resources=_provider("hq.domains.expenses.specs:resources"),
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
        ),
    ),
    DomainDescriptor(
        id="hq.receipts",
        label="Receipts",
        description="Receipts, matched to the expense or asset each paid for.",
        navigation=(NavigationItem("Receipts", "receipts:list", "receipts", 121, "Business"),),
        integration=PluginIntegration(
            capabilities=_provider("hq.domains.receipts.specs:capabilities"),
            resources=_provider("hq.domains.receipts.specs:resources"),
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
                ("update", "Change a receipt's details. The file itself does not change.", ""),
                ("delete", "Delete one receipt and its file.", ""),
            ),
            # Its match page links it to one of the expenses it may be for.
            variants=(("update", "expense", "Link to expense"), ("update", "link", "Link")),
        ),
    ),
    DomainDescriptor(
        id="hq.assets",
        label="Assets",
        description="What the business owns, what it cost and what it serves.",
        navigation=(NavigationItem("Assets", "assets:list", "assets", 122, "Business"),),
        integration=PluginIntegration(resources=_provider("hq.domains.assets.specs:resources")),
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
        ),
    ),
    DomainDescriptor(
        id="hq.reports",
        label="Reports",
        description="The year's figures, drawn from expenses, receipts and assets.",
        navigation=(NavigationItem("Reports", "reports:dashboard", "reports", 123, "Business"),),
        apps=("hq.domains.reports",),
        mounts=(Mount("reports/", "hq.domains.reports.urls"),),
    ),
    DomainDescriptor(
        id="hq.findings",
        label="Findings",
        description="What HQ finds wrong across the estate, with the evidence and a remedy.",
        navigation=(
            # Checks, the rules every finding comes from, opens from the
            # Findings page's own action rather than a nav entry of its own.
            NavigationItem(
                "Findings",
                "control_plane:findings",
                "control_plane",
                128,
                "Infrastructure",
            ),
        ),
    ),
    DomainDescriptor(
        id="hq.topology",
        label="Map",
        description="The infrastructure as one graph: what depends on what, and the questions it answers.",
        # The relational map leads the Infrastructure group because it is the
        # entry point that explains how every narrower workspace fits together.
        navigation=(
            NavigationItem(
                "Map",
                "control_plane:topology",
                "control_plane",
                129,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(resources=_provider("hq.platform.application.topology:resources")),
    ),
    DomainDescriptor(
        id="hq.services",
        label="Services",
        description="Every service by its name, and the state of its DNS, proxy and certificate.",
        # Ahead of Resources deliberately. A resource is a declaration a
        # controller reconciles; a service is the thing an operator was actually
        # thinking of when they opened this group. The registry stays one click
        # further in, where the answer is "which declaration is wrong".
        navigation=(
            NavigationItem(
                "Services",
                "control_plane:services",
                "control_plane",
                130,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.service_list:capabilities"),
            resources=_provider("hq.platform.application.service_list:resources"),
        ),
    ),
    DomainDescriptor(
        id="hq.estate",
        label="Estate",
        description="Machines, services, domains, renewals and connections, counted as one estate.",
        # No page of its own. Its card is machines, services, domains and
        # connections at once, each figure linking to the page that lists it.
        integration=PluginIntegration(
            dashboard=Viewed("hq.platform.application.estate:cards"),
            overview=Viewed("hq.platform.application.estate:overview"),
            calendars=_provider("hq.platform.application.calendar_sources:estate_sources"),
            resources=_provider("hq.platform.application.estate:resources"),
        ),
    ),
    DomainDescriptor(
        id="hq.infrastructure",
        label="Infrastructure",
        description="Every record HQ manages: what HQ expects of each and what is live.",
        navigation=(
            NavigationItem(
                "All records",
                "control_plane:list",
                "control_plane",
                131,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.infrastructure_specs:capabilities"),
            resources=_provider("hq.platform.application.infrastructure_specs:resources"),
        ),
        apps=("hq.domains.control_plane",),
        mounts=(Mount("infrastructure/", "hq.domains.control_plane.urls"),),
    ),
    DomainDescriptor(
        id="hq.tools",
        label="Tools",
        description="Lookups of what the outside world holds, such as public DNS.",
        # One entry, however many tools. Every other Infrastructure section
        # reads something HQ holds; these read what the world holds, which is
        # the question an operator has while looking at the rest. Tools are
        # tabs inside this rather than siblings beside it, so the twentieth
        # costs no more of the bar than the first.
        navigation=(
            NavigationItem(
                "Tools",
                "control_plane:tools",
                "control_plane",
                133,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.lookup:capabilities"),
            connections=_provider("hq.domains.control_plane.dns_lookup:connection_specs"),
        ),
    ),
    DomainDescriptor(
        id="hq.machines",
        label="Machines",
        description="The machines the estate runs on, as the services and agents on them report them.",
        # Between the things HQ manages and the credentials it manages them
        # with, because that is what a machine is: the thing both halves are
        # about.
        navigation=(
            NavigationItem(
                "Machines",
                "control_plane:machines",
                "control_plane",
                132,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(resources=_provider("hq.platform.application.machines:resources")),
    ),
    DomainDescriptor(
        id="hq.containers",
        label="Containers",
        description="The containers each machine runs, and whether each is current and safe.",
        # Beside machines, because a container is what a machine is for; the
        # question here is whether what runs on them is current and safe.
        navigation=(
            NavigationItem(
                "Containers",
                "control_plane:containers",
                "control_plane",
                132,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(resources=_provider("hq.platform.application.container_reads:resources")),
    ),
    DomainDescriptor(
        id="hq.tailnet",
        label="Tailnet",
        description="The private network the machines share: its policy, grants and members.",
        # After machines, because it is about the network they are all on
        # rather than about any one of them, and the answers it gives are
        # only meaningful once you know which machine you are asking about.
        navigation=(
            NavigationItem(
                "Tailnet",
                "control_plane:tailnet",
                "control_plane",
                133,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(resources=_provider("hq.platform.application.tailnet_context:resources")),
    ),
    DomainDescriptor(
        id="hq.connections",
        label="Connections",
        description="The credentials HQ reads each system through, and what each can see.",
        # Last in the group, and deliberately so. It answers "what can HQ reach
        # at all": the question underneath every other page here, and the one
        # asked least often, because the answer only changes when a credential
        # does.
        navigation=(
            NavigationItem(
                "Connections",
                "control_plane:connections",
                "control_plane",
                133,
                "Infrastructure",
            ),
        ),
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.connections:capabilities"),
            resources=_provider("hq.platform.application.connections:resources"),
            connections=_provider("hq.platform.application.connections:connection_specs"),
        ),
    ),
    DomainDescriptor(
        id="hq.events",
        label="Events",
        description="What providers and HQ's host told HQ happened, and the readings each made due.",
        # No place in the menu: the controller's page leads to it, beside the
        # listener's standing.
        integration=PluginIntegration(resources=_provider("hq.platform.application.inbound_reads:resources")),
    ),
    DomainDescriptor(
        id="hq.jobs",
        label="Jobs",
        description="HQ's background jobs and their runs.",
        navigation=(NavigationItem("Background jobs", "jobs:list", "jobs", 132, "Infrastructure"),),
        # A scan HQ asks for is listed with the rest of its runner's work,
        # and Adopt makes a scan result the adopted playbooks.
        integration=PluginIntegration(
            capabilities=_provider("hq.platform.application.automation_adoption:capability_specs"),
            resources=_provider("hq.platform.application.run_reads:resources"),
            outbound=_provider("hq.platform.application.host_work:outbound"),
        ),
        apps=("hq.domains.jobs",),
        mounts=(Mount("jobs/", "hq.domains.jobs.urls"),),
    ),
    DomainDescriptor(
        id="hq.procedures",
        label="Playbooks",
        description="The playbooks HQ can run, and the scans of your repository you adopt them from.",
        # Beside the jobs that run them: the work a playbook is, before it runs.
        navigation=(NavigationItem("Playbooks", "control_plane:playbooks", "control_plane", 132, "Infrastructure"),),
        integration=PluginIntegration(resources=_provider("hq.platform.application.playbook_reads:resources")),
    ),
    DomainDescriptor(
        id="hq.audit",
        label="Audit",
        description="Every security-relevant change HQ made, who asked for it and when.",
        navigation=(NavigationItem("Audit log", "core:audit_list", "core", HOST_ORDER_MACHINERY, "System"),),
        integration=PluginIntegration(resources=_provider("hq.platform.application.audit_specs:resources")),
        mounts=(Mount("audit/", "hq.platform.core.urls"),),
    ),
    DomainDescriptor(
        id="hq.approvals",
        label="Waiting for approval",
        description="Changes held until an operator approves them.",
        # No page in the navigation: a held change is decided on its
        # approval's page, which each item opens, and all of them in the
        # audit log's approval queue, which the queue's heading opens.
        integration=PluginIntegration(
            attention_url=_provider("hq.platform.application.attention:approval_queue"),
            capabilities=_provider("hq.platform.application.held_reads:capabilities"),
            resources=_provider("hq.platform.application.held_reads:resources"),
        ),
    ),
    DomainDescriptor(
        id="hq.history",
        label="History",
        description="What happened, as dates on the calendar.",
        # No page of its own: what happened is read on the calendar, a day at
        # a time, and in full on the audit log.
        integration=PluginIntegration(calendars=_provider("hq.platform.application.calendar_sources:history_sources")),
    ),
    DomainDescriptor(
        id="hq.search",
        label="Search",
        description="One query across every domain, answered with what the reader may read.",
        # No nav item: the bar's own search field opens its page.
        integration=PluginIntegration(resources=_provider("hq.platform.application.search:resources")),
    ),
    DomainDescriptor(
        id="hq.self",
        label="HQ",
        description="How HQ itself is doing, read as its own page shows it.",
        # HQ itself, after what configures it: the page that says how it is doing.
        navigation=(NavigationItem("HQ", "hq_self", "", HOST_ORDER_MACHINERY + 3, "System"),),
        integration=PluginIntegration(resources=_provider("hq.platform.application.hq_context:resources")),
    ),
    DomainDescriptor(
        id="hq.errors",
        label="Errors",
        description="The unhandled errors of the requests HQ failed, each by the request id its error page printed.",
        # Beside HQ itself: what went wrong while it served.
        navigation=(NavigationItem("Errors", "errors:list", "errors", HOST_ORDER_MACHINERY + 4, "System"),),
        integration=PluginIntegration(resources=_provider("hq.platform.application.error_reads:resources")),
        mounts=(Mount("errors/", "hq.platform.core.error_log_urls"),),
    ),
    DomainDescriptor(
        id="hq.design",
        label="Design",
        description="Every shared part HQ draws its pages from, filled with example data.",
        # Last of HQ's own machinery: the parts every page above is drawn from.
        navigation=(NavigationItem("Design", "design", "", HOST_ORDER_MACHINERY + 5, "System"),),
    ),
    DomainDescriptor(
        id="hq.agents",
        label="Agents",
        description="What each agent may do, and the brakes on it.",
        navigation=(NavigationItem("Agents", "agent_policy", "", HOST_ORDER_MACHINERY + 1, "System"),),
        integration=PluginIntegration(capabilities=_provider("hq.platform.application.agent_access:capabilities")),
    ),
    DomainDescriptor(
        id="hq.api",
        label="API",
        description="The routes every domain is reached through: the root, the catalogs of commands and reads, this "
        "document, and the generic form of each command and read.",
        # Beside Agents: the machine API is what an agent's token reaches.
        navigation=(
            NavigationItem(
                "API",
                "api_reference:reference",
                "api_reference",
                HOST_ORDER_MACHINERY + 2,
                "System",
            ),
        ),
        mounts=(Mount("api/docs/", "hq.platform.api.web_urls"),),
    ),
)
