"""Canonical operating snapshot for HQ delivery adapters.

Assembly only. Every figure, row and queue entry below is a section's own
answer, asked once and named here for transport: this module imports no
model and decides no number.
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from functools import partial
from typing import Any, TypedDict

from django.utils import timezone

from . import sections
from .attention import finding_insight, readable_by
from .derivations import derivation
from .derived_inputs import DASHBOARD_READS, QUEUE_READS, READER_QUEUE_READS, composed_variant
from .domains import (
    all_domains,
    domain_attention_items,
    domain_dashboard_cards,
    domain_dashboard_sections,
    provided,
    reader_variant,
)
from .finding_model import Finding
from .plugins import source_overview
from .projection import projection_scope
from .read_models import ActivityEvent, recent_activity
from .record_findings import contacts_state
from .security import Capability, Principal
from .workflows import WrittenAction, WrittenWorkflow, serialize_workflow
lazy from .action_items import item_key, item_revision, waiting_count
lazy from .card_items import Insight
lazy from .routes import reverse
lazy from .ui import DomainOverview


@derivation("dashboard.highlights", reads=DASHBOARD_READS, vary=reader_variant)
def dashboard_highlights(principal: Principal) -> dict[str, Any]:
    """Group headline readings and optional visuals from existing contracts, as ``principal`` may read them.

    Derived once per change and set of capabilities: each extension's cards
    and overview are derivations of their own, so this composes what is
    stored.
    """

    domains = {domain.id: domain for domain in all_domains()}
    highlights, compact = [], []
    for section in domain_dashboard_sections(principal):
        overview = _overview(domains[section["id"]], principal) if len(section["cards"]) > 1 else None
        if not _holds_a_card(section["cards"], overview):
            compact.extend(section["cards"])
            continue
        highlights.append({**section, "overview": overview})
    return {"highlights": highlights, "compact": compact}


# A highlight card pairs with the one beside it. Fewer figures than this leave
# it a line beside that card's rows, so they read in the strip instead.
CARD_FIGURES = 3


def _overview(domain, principal: Principal) -> DomainOverview | None:
    provider = domain.integration.overview
    return source_overview(domain.id, partial(provided, provider, principal)) if provider else None


def _holds_a_card(cards, overview: DomainOverview | None) -> bool:
    """Whether a section fills a card of its own: a chart to lead with, or
    figures enough to stand level with the card beside it."""

    if overview is not None and overview.charts:
        return True
    figures = overview.kpis if overview is not None and overview.kpis else cards
    return len(figures) >= CARD_FIGURES


@derivation("attention.work", reads=QUEUE_READS, vary=composed_variant)
def work_queue() -> list[QueueItem]:
    """The composed queue, flattened for transport, derived once per change.

    Projected from ``domain_attention_items`` rather than assembled here: the
    domains own what needs doing, and this is only the shape it travels in.
    ``url`` rides along so no consumer needs a table to turn an entry back into
    a link. The host's part and each extension's are derived on their own, so
    a change to one composes the rest from what is stored.
    """

    return gathered_rows(domain_attention_items())


def _reader(principal: Principal) -> tuple[Any, ...]:
    # Who, as well as what they may read: a credential reads only its own person's held requests,
    # and a held request is never offered to its asker.
    return (principal.actor, principal.interface, principal.person_key, *reader_variant(principal))


@derivation("attention.readable", reads=READER_QUEUE_READS, vary=_reader)
def readable_queue(principal: Principal) -> list[QueueItem]:
    """The composed queue as ``principal`` may read it (``attention.readable_by``),
    derived once per change, the one way each door reads it."""

    return readable_by(principal, work_queue())


def _count_variant(principal: Principal, user_pk: int) -> tuple[Any, ...]:
    return (user_pk, *_reader(principal))


@derivation("attention.count", reads=(*READER_QUEUE_READS, "core.ActionItemRead"), vary=_count_variant)
def waiting(principal: Principal, user_pk: int) -> int:
    """How many items wait on one person: the queue as they may read it, less what they set aside."""

    return waiting_count(readable_queue(principal), user_pk)


class QueueItem(TypedDict):
    """One action item as the queue partial and the machine doors carry it."""

    key: str
    revision: str
    source_id: str
    source: str
    label: str
    detail: str
    count: int
    status: str
    url: str
    action: str
    workflow: WrittenWorkflow | None
    actions: list[WrittenAction]
    subject: WrittenAction | None
    notice: bool
    # A request a person decides, never set aside (``Insight.held``).
    held: bool
    family: str
    # Why it is as urgent as its status says (``Insight.why``).
    why: str
    # What clears it, or "" (``Insight.clears``).
    clears: str
    # ISO 8601, or "" when unknown.
    since: str
    observed_at: str
    # The kind of reading observed_at dates, or "".
    reads: str
    # Each a label and its value.
    facts: list[list[str]]
    links: list[WrittenAction]
    read_capabilities: list[str]


def finding_rows(source_id: str, source: str, findings: Iterable[Finding]) -> tuple[QueueItem, ...]:
    """``findings`` as the queue's rows, each worded by ``attention.finding_insight``:
    how a page draws what is wrong with the thing it is about."""

    url = reverse("control_plane:findings")
    return tuple(_queue_item(source_id, source, finding_insight(finding, url)) for finding in findings)


def gathered_rows(entries: Iterable[Mapping[str, Any]]) -> list[QueueItem]:
    """Entries the queue gathered (``plugins.gather_attention``), each its
    finding as ``finding_rows`` words it, in the order gathered."""

    return [row for entry in entries for row in finding_rows(entry["source_id"], entry["source"], (entry["finding"],))]


def _queue_item(source_id: str, source: str, item: Insight) -> QueueItem:
    # Private, so a row reaches a page only as a finding (``finding_rows``).

    return {
        "key": item_key(source_id, item.key),
        "revision": item_revision(item),
        "source_id": source_id,
        "source": source,
        "label": item.title,
        "detail": item.body,
        "count": item.magnitude or 1,
        "status": item.status,
        "url": item.url,
        "action": item.action,
        "workflow": serialize_workflow(item.workflow),
        "actions": [asdict(action) for action in item.actions],
        "subject": asdict(item.subject) if item.subject else None,
        "notice": item.notice,
        "held": item.held,
        "family": item.family,
        "why": item.why,
        "clears": item.clears,
        "since": item.since.isoformat() if item.since else "",
        "observed_at": item.observed_at.isoformat() if item.observed_at else "",
        "reads": item.reads,
        "facts": [list(fact) for fact in item.facts],
        "links": [asdict(link) for link in item.links],
        "read_capabilities": list(item.read_capabilities),
    }


class Upstreams(TypedDict):
    # Whether the last read of the contact form's store reached it.
    contacts: str


class Kpis(TypedDict):
    active_projects: int
    projects_needing_output: int
    draft_content: int
    published_content: int
    unread_contacts: int
    # Money as its decimal text.
    expenses_total: str
    expenses_count: int
    deductible_total: str


class _Activity(TypedDict):
    recent_activity: list[ActivityEvent]
    # Each part this caller may not read, said, so withheld and empty read apart.
    unread: list[str]


class Glance(_Activity):
    """The snapshot but its queue, which each reader carries in its own shape."""

    upstreams: Upstreams
    year: int
    kpis: Kpis
    cards: list[dict[str, Any]]
    priority_count: int
    active_projects: list[dict[str, Any]]
    draft_content: list[dict[str, Any]]
    recent_published: list[dict[str, Any]]


class _Snapshot(Glance):
    priority: list[QueueItem]


class OperatingSnapshot(_Snapshot):
    """What ``operating_snapshot`` answers."""

    generated_at: str


def operating_snapshot(*, principal) -> OperatingSnapshot:
    """Return the one canonical KPI, work-queue, and activity projection.

    Recent activity is audit data, read only for a principal that may read
    the audit log. For any other, ``recent_activity`` is empty and ``unread``
    says it was withheld, so withheld and empty read differently; ``unread``
    is empty when nothing was left out.
    """
    with projection_scope():
        # When it was asked for, not when it was derived.
        return {"generated_at": timezone.now().isoformat(), **_operating_snapshot(principal)}


def _snapshot_variant(principal) -> tuple[Any, ...]:
    """Who is reading, since the audit lines are theirs to see or not."""

    return (
        principal.actor,
        principal.interface,
        # Whose held requests its queue holds (``readable_queue``).
        principal.person_key,
        tuple(sorted(str(item) for item in principal.capabilities)),
        *composed_variant(),
    )


# The operating snapshot: the queue, the cards, each record domain's recent
# rows and the audit lines.
_SNAPSHOT_READS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *READER_QUEUE_READS,
            *DASHBOARD_READS,
            "content.ContentItem_related_projects",
            "core.AuditLog",
        )
    )
)


@derivation("dashboard.snapshot", reads=_SNAPSHOT_READS, vary=_snapshot_variant)
def _operating_snapshot(principal) -> _Snapshot:
    unread_contacts_count, contacts_status = contacts_state()
    projects = sections.projects_reading()
    content = sections.content_reading()
    expenses = sections.expenses_reading()
    priority = readable_queue(principal)

    return {
        "upstreams": {"contacts": contacts_status},
        "year": expenses["year"],
        # Every figure here is a section's own answer, asked once above. This
        # block names them for transport; it does not decide any of them.
        "kpis": {
            "active_projects": projects["active"],
            "projects_needing_output": projects["needing_output"],
            "draft_content": content["drafts"],
            "published_content": content["published"],
            "unread_contacts": unread_contacts_count,
            "expenses_total": str(expenses["total"]),
            "expenses_count": expenses["count"],
            "deductible_total": str(expenses["deductible"]),
        },
        # Every domain's headline reading, host and extension alike, already in
        # the order the nav presents them. Carried here so a delivery adapter
        # asks for the dashboard once rather than assembling it from two calls.
        "cards": list(domain_dashboard_cards(principal)),
        "priority": priority,
        # Items, not the numbers they carry: each item shows its own.
        "priority_count": len(priority),
        "active_projects": sections.recent_active_projects(),
        "draft_content": sections.recent_draft_content(),
        "recent_published": sections.recently_published(),
        **_activity(principal),
    }


# A part an answer leaves out is said in its ``unread``, as the path reads say it.
WITHHELD_ACTIVITY = "not read: recent_activity, because this caller may not read the audit log"


def _activity(principal) -> _Activity:
    if principal.permits(Capability.READ_AUDIT_LOG):
        return {"recent_activity": recent_activity(limit=8)["items"], "unread": []}
    return {"recent_activity": [], "unread": [WITHHELD_ACTIVITY]}
