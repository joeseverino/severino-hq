"""Canonical operating snapshot for HQ delivery adapters.

Assembly only. Every figure, row and queue entry below is a section's own
answer, asked once and named here for transport: this module imports no
model and decides no number.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.utils import timezone

from . import sections
from .attention import contacts_state
from .derivations import derivation
from .derived_inputs import DASHBOARD_READS, QUEUE_READS, composed_variant
from .domains import (
    all_domains,
    domain_attention_items,
    domain_dashboard_cards,
    domain_dashboard_sections,
)
from .projection import projection_scope
from .read_models import recent_activity
from .security import Capability
from .workflows import serialize_workflow


@derivation("dashboard.highlights", reads=DASHBOARD_READS, vary=composed_variant)
def dashboard_highlights() -> dict[str, Any]:
    """Group headline readings and optional visuals from existing contracts.

    Derived once per change: each extension's cards and overview are
    derivations of their own, so this composes what is stored.
    """
    from django.core.exceptions import ImproperlyConfigured

    from .ui import DomainOverview

    domains = {domain.id: domain for domain in all_domains()}
    highlights, compact = [], []
    for section in domain_dashboard_sections():
        if len(section["cards"]) == 1:
            compact.extend(section["cards"])
            continue
        provider = domains[section["id"]].integration.overview
        overview = provider() if provider else None
        if overview is not None and not isinstance(overview, DomainOverview):
            raise ImproperlyConfigured("Domain overview must return DomainOverview.")
        highlights.append({**section, "overview": overview})
    return {"highlights": highlights, "compact": compact}


@derivation("attention.work", reads=QUEUE_READS, vary=composed_variant)
def work_queue() -> list[dict[str, Any]]:
    """The composed queue, flattened for transport, derived once per change.

    Projected from ``domain_attention_items`` rather than assembled here: the
    domains own what needs doing, and this is only the shape it travels in.
    ``url`` rides along so no consumer needs a table to turn an entry back into
    a link. The host's part and each extension's are derived on their own, so
    a change to one composes the rest from what is stored.
    """

    return [
        queue_item(entry["source_id"], entry["source"], entry["item"])
        for entry in domain_attention_items()
    ]


def _count_variant(user_pk: int) -> tuple[Any, ...]:
    return (user_pk, *composed_variant())


@derivation(
    "attention.count", reads=(*QUEUE_READS, "core.ActionItemRead"), vary=_count_variant
)
def waiting(user_pk: int) -> int:
    """How many items wait on one person: the queue, less what they set aside."""

    from .action_items import waiting_count

    return waiting_count(work_queue(), user_pk)


def queue_item(source_id: str, source: str, item: Any) -> dict[str, Any]:
    """One action item in the shape the queue partial and the API carry."""

    from .action_items import item_key, item_revision

    subject = getattr(item, "subject", None)
    since = getattr(item, "since", None)
    return {
        "key": item_key(source_id, item),
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
        "subject": asdict(subject) if subject else None,
        "notice": bool(getattr(item, "notice", False)),
        "family": getattr(item, "family", ""),
        "since": since.isoformat() if since else "",
    }


def operating_snapshot(*, principal) -> dict[str, Any]:
    """Return the one canonical KPI, work-queue, and activity projection.

    Recent activity is audit data; it is included only for a principal that
    may read the audit log.
    """
    with projection_scope():
        # When it was asked for, not when it was derived.
        return {"generated_at": timezone.now().isoformat(), **_operating_snapshot(principal)}


def _snapshot_variant(principal) -> tuple[Any, ...]:
    """Who is reading, since the audit lines are theirs to see or not."""

    return (
        principal.actor,
        principal.interface,
        tuple(sorted(str(item) for item in principal.capabilities)),
        *composed_variant(),
    )


# The operating snapshot: the queue, the cards, each record domain's recent
# rows and the audit lines.
_SNAPSHOT_READS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *QUEUE_READS,
            *DASHBOARD_READS,
            "content.ContentItem_related_projects",
            "core.AuditLog",
        )
    )
)


@derivation("dashboard.snapshot", reads=_SNAPSHOT_READS, vary=_snapshot_variant)
def _operating_snapshot(principal) -> dict[str, Any]:
    unread_contacts_count, contacts_status = contacts_state()
    projects = sections.projects_reading()
    content = sections.content_reading()
    documentation = sections.documentation_reading()
    expenses = sections.expenses_reading()
    priority = work_queue()

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
            "docs_needing_review": documentation["needing_review"],
            "unread_contacts": unread_contacts_count,
            "expenses_total": str(expenses["total"]),
            "expenses_count": expenses["count"],
            "deductible_total": str(expenses["deductible"]),
        },
        # Every domain's headline reading, host and extension alike, already in
        # the order the nav presents them. Carried here so a delivery adapter
        # asks for the dashboard once rather than assembling it from two calls.
        "cards": list(domain_dashboard_cards()),
        "priority": priority,
        # Items, not the numbers they carry: each item shows its own.
        "priority_count": len(priority),
        "active_projects": sections.recent_active_projects(),
        "draft_content": sections.recent_draft_content(),
        "recent_published": sections.recently_published(),
        "docs_needing_review": sections.docs_awaiting_review(),
        "recent_activity": (
            recent_activity(principal=principal, limit=8)["items"]
            if principal.permits(Capability.READ_AUDIT_LOG)
            else []
        ),
    }
