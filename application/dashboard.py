"""Canonical operating snapshot for HQ delivery adapters.

Assembly only. Every figure, row and queue entry below is a section's own
answer, asked once and named here for transport -- this module imports no
model and decides no number. It previously queried eight of them directly,
which meant a change to what a section counts had to be made here as well as
wherever the section itself counted it.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from . import sections
from .attention import contacts_state
from .domains import domain_attention_items, domain_dashboard_cards
from .read_models import recent_activity


def work_queue() -> list[dict[str, Any]]:
    """The composed queue, flattened for transport.

    Projected from ``domain_attention_items`` rather than assembled here: the
    domains own what needs doing, and this is only the shape it travels in.
    ``url`` rides along so no consumer needs a table to turn an entry back into
    a link.
    """

    return [
        {
            "source_id": entry["source_id"],
            "source": entry["source"],
            "label": entry["item"].title,
            "detail": entry["item"].body,
            "count": entry["item"].magnitude or 1,
            "status": entry["item"].status,
            "url": entry["item"].url,
        }
        for entry in domain_attention_items()
    ]


def operating_snapshot() -> dict[str, Any]:
    """Return the one canonical KPI, work-queue, and activity projection."""
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
        "recent_activity": recent_activity(limit=8)["items"],
    }
