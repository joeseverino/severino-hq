"""Each host section's own figures, computed once.

A section states its readings in one ``*_reading`` function. Everything that
needs those numbers derives from it: the dashboard card row, and the snapshot's
KPI block that the MCP and the section panels read. Nothing recomputes a figure
a section has already answered, so changing what "needs output" means is one
edit rather than a hunt for every place that counted it.

Cards are the display projection of a reading -- ``{id, label, value, url}``
plus optional ``detail``, the same shape an extension emits, so the dashboard
renders host and extension cards through one loop and a new section needs no
template change to appear.

A section with nothing to report returns no card. That is the whole of the
"dormant section" behaviour: a section carrying no data stops occupying a tile
on the page checked every day, and lights up on its own the moment it has
something to say. No flag, no configuration, no decision to revisit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project

from .projection import read_once
from .services import service_reading

Card = dict[str, Any]
ZERO_MONEY = Decimal("0.00")


def _card(
    *, id: str, label: str, value: str, url: str, detail: str = ""
) -> tuple[Card, ...]:
    """One card, or none at all when the section has nothing to report."""

    card: Card = {"id": id, "label": label, "value": value, "url": url}
    if detail:
        card["detail"] = detail
    return (card,)


# ----- Projects --------------------------------------------------------------


def active_projects():
    return Project.objects.filter(status=Project.Status.ACTIVE)


def projects_needing_output():
    """Active work with neither content nor documentation recorded against it."""

    return (
        active_projects()
        .annotate(
            content_count=Count("content_items", distinct=True),
            doc_count=Count("documentation_records", distinct=True),
        )
        .filter(Q(content_count=0) | Q(doc_count=0))
    )


def projects_reading() -> dict[str, int]:
    return read_once(
        "sections.projects",
        lambda: {
            "active": active_projects().count(),
            "needing_output": projects_needing_output().count(),
        },
    )


def projects() -> tuple[Card, ...]:
    reading = projects_reading()
    if not reading["active"]:
        return ()
    return _card(
        id="hq.projects.active",
        label="Active projects",
        value=str(reading["active"]),
        url=reverse("projects:list"),
        detail=(
            f"{reading['needing_output']} need output"
            if reading["needing_output"]
            else ""
        ),
    )


# ----- Content ---------------------------------------------------------------


def draft_content():
    return ContentItem.objects.filter(status=ContentItem.Status.DRAFT)


def published_content():
    return ContentItem.objects.filter(status=ContentItem.Status.PUBLISHED)


def content_reading() -> dict[str, int]:
    return read_once(
        "sections.content",
        lambda: {
            "drafts": draft_content().count(),
            "published": published_content().count(),
        },
    )


def content() -> tuple[Card, ...]:
    reading = content_reading()
    if not reading["drafts"] and not reading["published"]:
        return ()
    return _card(
        id="hq.content.drafts",
        label="Draft content",
        value=str(reading["drafts"]),
        url=f"{reverse('content:list')}?status=draft",
        detail=f"{reading['published']} published" if reading["published"] else "",
    )


# ----- Documentation ---------------------------------------------------------


def docs_needing_review():
    return DocumentationRecord.objects.needing_review()


def documentation_reading() -> dict[str, int]:
    return read_once(
        "sections.documentation",
        lambda: {"needing_review": docs_needing_review().count()},
    )


def documentation() -> tuple[Card, ...]:
    reading = documentation_reading()
    if not reading["needing_review"]:
        return ()
    return _card(
        id="hq.docs.needing_review",
        label="Docs to review",
        value=str(reading["needing_review"]),
        url=f"{reverse('docs_index:list')}?needs_review=1",
    )


# ----- Expenses --------------------------------------------------------------


def fiscal_year_start(today=None):
    """The first day of the fiscal year containing ``today``.

    Defined here rather than at each call site: the year-to-date totals and any
    report that has to agree with them must start counting on the same day.
    """

    today = today or timezone.localdate()
    start_month = getattr(settings, "SEVERINO_FISCAL_YEAR_START_MONTH", 1)
    start = today.replace(month=start_month, day=1)
    if start > today:
        start = start.replace(year=today.year - 1)
    return start


def _expenses_reading() -> dict[str, Any]:
    today = timezone.localdate()
    totals = Expense.objects.filter(
        date__range=(fiscal_year_start(today), today)
    ).aggregate(
        total=Sum("total_cost"),
        deductible=Sum("estimated_deductible_amount"),
        count=Count("id"),
    )
    return {
        "count": totals["count"] or 0,
        "total": totals["total"] or ZERO_MONEY,
        "deductible": totals["deductible"] or ZERO_MONEY,
        "year": today.year,
    }


def expenses_reading() -> dict[str, Any]:
    return read_once("sections.expenses", _expenses_reading)


def expenses() -> tuple[Card, ...]:
    reading = expenses_reading()
    if not reading["count"]:
        return ()
    return _card(
        id="hq.expenses.ytd",
        label=f"Expenses {reading['year']}",
        value=f"${reading['total']:,.2f}",
        url=reverse("expenses:list"),
        detail=f"${reading['deductible']:,.2f} deductible est.",
    )


# ----- Services --------------------------------------------------------------
#
# The reading itself lives beside its derivation in ``application.services``.
# Every other section here reads one table and can state its own figures; a
# service is a join across three, and splitting the count from the join would
# put half of one answer in each file.


def services() -> tuple[Card, ...]:
    reading = service_reading()
    if not reading["total"]:
        return ()
    return _card(
        id="hq.services.total",
        label="Services",
        value=str(reading["total"]),
        url=reverse("control_plane:services"),
        detail=(
            f"{reading['incomplete']} incompletely wired"
            if reading["incomplete"]
            else ""
        ),
    )


# ----- Recent rows -----------------------------------------------------------
#
# Each section decides which of its own fields summarise one of its records,
# and how many of them a glance is worth. Held here beside the querysets they
# read, so a field added to a summary is one edit in the section that owns it.

ROW_LIMIT = 4


def _project(project: Project) -> dict[str, Any]:
    return {
        "slug": project.slug,
        "name": project.name,
        "category": project.category,
        "category_label": project.get_category_display(),
        "repository_url": project.repository_url,
        "public_url": project.public_url,
        "updated_at": project.updated_at.isoformat(),
    }


def _content(item: ContentItem) -> dict[str, Any]:
    return {
        "slug": item.slug,
        "title": item.title,
        "content_type_label": item.get_content_type_display(),
        "published_url": item.published_url,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "updated_at": item.updated_at.isoformat(),
    }


def _documentation(record: DocumentationRecord) -> dict[str, Any]:
    return {
        "doc_id": record.doc_id,
        "title": record.title,
        "last_reviewed": record.last_reviewed.isoformat() if record.last_reviewed else None,
    }




def recent_active_projects() -> list[dict[str, Any]]:
    return [
        _project(project)
        for project in active_projects().order_by("-updated_at")[:ROW_LIMIT]
    ]


def recent_draft_content() -> list[dict[str, Any]]:
    return [
        _content(item)
        for item in draft_content().order_by("-updated_at")[:ROW_LIMIT]
    ]


def recently_published() -> list[dict[str, Any]]:
    return [
        _content(item)
        for item in published_content().order_by("-published_at", "-updated_at")[
            :ROW_LIMIT
        ]
    ]


def docs_awaiting_review() -> list[dict[str, Any]]:
    return [
        _documentation(record)
        for record in docs_needing_review().order_by("last_reviewed")[:ROW_LIMIT]
    ]
