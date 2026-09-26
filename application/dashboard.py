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


def dashboard_highlights() -> dict[str, Any]:
    """Group headline readings and optional visuals from existing contracts."""
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


def work_queue() -> list[dict[str, Any]]:
    """The composed queue, flattened for transport.

    Projected from ``domain_attention_items`` rather than assembled here: the
    domains own what needs doing, and this is only the shape it travels in.
    ``url`` rides along so no consumer needs a table to turn an entry back into
    a link.
    """

    from .action_items import item_key, item_revision

    return [
        {
            "key": item_key(entry["source_id"], entry["item"]),
            "revision": item_revision(entry["item"]),
            "source_id": entry["source_id"],
            "source": entry["source"],
            "label": entry["item"].title,
            "detail": entry["item"].body,
            "count": entry["item"].magnitude or 1,
            "status": entry["item"].status,
            "url": entry["item"].url,
            "action": entry["item"].action,
            "workflow": serialize_workflow(entry["item"].workflow),
            "actions": [asdict(action) for action in entry["item"].actions],
            "subject": (
                asdict(subject)
                if (subject := getattr(entry["item"], "subject", None))
                else None
            ),
        }
        for entry in domain_attention_items()
    ]


def operating_snapshot(*, principal) -> dict[str, Any]:
    """Return the one canonical KPI, work-queue, and activity projection.

    Recent activity is audit data; it is included only for a principal that
    may read the audit log.
    """
    with projection_scope():
        return _operating_snapshot(principal)


def _operating_snapshot(principal) -> dict[str, Any]:
    unread_contacts_count, contacts_status = contacts_state()
    projects = sections.projects_reading()
    content = sections.content_reading()
    documentation = sections.documentation_reading()
    expenses = sections.expenses_reading()
    priority = work_queue()

    return {
        "generated_at": timezone.now().isoformat(),
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
        "priority_count": sum(item["count"] for item in priority),
        "priority_group_count": len(priority),
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
