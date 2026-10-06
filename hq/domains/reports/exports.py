"""
Exports: CSV per entity, plus a year-summary in JSON and Markdown.

The Markdown export is designed to be AI-readable; the JSON export uses stable
internal IDs/slugs so a future MCP can reason about relationships.

No secrets, no sensitive doc bodies, no receipt file contents: by design.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from operator import attrgetter
from typing import Any

from django.db.models import Sum
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.ui import MISSING


# ---------- CSV ----------------------------------------------------------------

def _csv_response(headers: list[str], rows: Iterable[Iterable]) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    return buf.getvalue()


def _csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


# A column is a field name, read as that attribute, or (header, reader) where
# the cell is derived. Each export states its columns once; the header row and
# every data row come from the same tuple, so they cannot drift apart.
Column = str | tuple[str, Callable[[Any], Any]]


def _csv_of(records: Iterable[Any], columns: tuple[Column, ...]) -> str:
    readers = [
        (column, attrgetter(column)) if isinstance(column, str) else column
        for column in columns
    ]
    return _csv_response(
        [header for header, _ in readers],
        ([read(record) for _, read in readers] for record in records),
    )


def _slug_of(relation: str, field: str = "slug") -> tuple[str, Callable[[Any], Any]]:
    """A related record's handle, or blank when there is none."""

    def read(record):
        if not getattr(record, f"{relation}_id"):
            return ""
        return getattr(getattr(record, relation), field)

    return relation, read


EXPENSE_COLUMNS: tuple[Column, ...] = (
    "id", "date", "vendor", "item", "category",
    "total_cost", "business_use_percentage", "estimated_deductible_amount",
    "payment_method", "business_purpose",
    _slug_of("related_project"), _slug_of("related_asset"), _slug_of("related_content"),
    _slug_of("related_documentation", "doc_id"),
    "notes",
)
ASSET_COLUMNS: tuple[Column, ...] = (
    "id", "slug", "item_name", "vendor", "category",
    "purchase_date", "total_cost",
    "business_use_percentage", "estimated_deductible_amount",
    "payment_method", "serial_number", "warranty_date", "status", "notes",
)
CONTENT_COLUMNS: tuple[Column, ...] = (
    "id", "slug", "title", "content_type", "status", "topic", "tags",
    "published_url", "published_at",
    ("wordpress_post_id", lambda c: c.wordpress_post_id or ""), "wordpress_slug",
)
PROJECT_COLUMNS: tuple[Column, ...] = (
    "id", "slug", "name", "category", "status",
    "repository_url", "public_url", "technologies_used",
)
DOCUMENTATION_COLUMNS: tuple[Column, ...] = (
    "doc_id", "title", "doc_type", "system_service", "environment",
    "status", "sensitivity",
    "obsidian_path", "github_path", "external_url", "last_reviewed",
)


def expenses_csv(year: int | None = None) -> str:
    qs = Expense.objects.all()
    if year:
        qs = qs.filter(date__year=year)
    qs = qs.order_by("date").select_related(
        "related_project", "related_asset", "related_content", "related_documentation",
    )
    return _csv_of(qs, EXPENSE_COLUMNS)


def assets_csv(year: int | None = None) -> str:
    qs = Asset.objects.all()
    if year:
        qs = qs.filter(purchase_date__year=year)
    return _csv_of(qs.order_by("-purchase_date"), ASSET_COLUMNS)


def content_csv() -> str:
    return _csv_of(ContentItem.objects.all().order_by("-updated_at"), CONTENT_COLUMNS)


def projects_csv() -> str:
    return _csv_of(Project.objects.all().order_by("-updated_at"), PROJECT_COLUMNS)


def documentation_csv() -> str:
    return _csv_of(
        DocumentationRecord.objects.all().order_by("doc_id"), DOCUMENTATION_COLUMNS
    )


# ---------- Year summary -------------------------------------------------------

def _money(value) -> str:
    return f"{(value or Decimal('0.00')):.2f}"


def year_summary(year: int) -> dict:
    # Related records are read from the prefetch (``.all()``): a ``values_list``
    # on the relation asks the database again for every row.
    expenses = Expense.objects.filter(date__year=year)
    assets = Asset.objects.filter(purchase_date__year=year)

    by_category = list(
        expenses.values("category")
        .annotate(total=Sum("total_cost"), deductible=Sum("estimated_deductible_amount"))
        .order_by("-total")
    )

    largest = list(
        expenses.order_by("-total_cost").values(
            "id", "date", "vendor", "item", "category", "total_cost",
        )[:10]
    )

    projects = [
        {
            "slug": p.slug,
            "name": p.name,
            "category": p.category,
            "status": p.status,
            "technologies": p.tech_list,
            "repository_url": p.repository_url,
            "public_url": p.public_url,
        }
        for p in Project.objects.all().order_by("name")
    ]

    content = [
        {
            "slug": c.slug,
            "title": c.title,
            "type": c.content_type,
            "status": c.status,
            "topic": c.topic,
            "tags": c.tag_list,
            "published_url": c.published_url,
            "published_at": c.published_at.isoformat() if c.published_at else None,
            "wordpress_post_id": c.wordpress_post_id,
            "related_projects": [related.slug for related in c.related_projects.all()],
            "related_assets": [related.slug for related in c.related_assets.all()],
            "related_documentation": [related.doc_id for related in c.related_documentation.all()],
        }
        for c in ContentItem.objects.all()
        .prefetch_related("related_projects", "related_assets", "related_documentation")
        .order_by("-updated_at")
    ]

    docs = [
        {
            "doc_id": d.doc_id,
            "title": d.title,
            "type": d.doc_type,
            "system_service": d.system_service,
            "environment": d.environment,
            "status": d.status,
            "sensitivity": d.sensitivity,
            "obsidian_path": d.obsidian_path,
            "github_path": d.github_path,
            "external_url": d.external_url,
            "last_reviewed": d.last_reviewed.isoformat() if d.last_reviewed else None,
            "safe_for_ai_export": d.is_safe_for_ai_export,
            "related_projects": [related.slug for related in d.related_projects.all()],
            "related_assets": [related.slug for related in d.related_assets.all()],
        }
        for d in DocumentationRecord.objects.all().prefetch_related(
            "related_projects", "related_assets"
        )
        .order_by("doc_id")
    ]

    asset_records = [
        {
            "slug": a.slug,
            "item_name": a.item_name,
            "vendor": a.vendor,
            "category": a.category,
            "purchase_date": a.purchase_date.isoformat() if a.purchase_date else None,
            "total_cost": _money(a.total_cost),
            "business_use_percentage": a.business_use_percentage,
            "estimated_deductible_amount": _money(a.estimated_deductible_amount),
            "status": a.status,
            "related_projects": [related.slug for related in a.related_projects.all()],
        }
        for a in assets.prefetch_related("related_projects")
    ]

    return {
        "generated_at": timezone.now().isoformat(),
        "year": year,
        "disclaimer": (
            "Estimated deductible = cost × business-use %. Estimate, not tax "
            "advice."
        ),
        "totals": {
            "expenses_count": expenses.count(),
            "expenses_total": _money(
                expenses.aggregate(s=Sum("total_cost"))["s"]
            ),
            "expenses_deductible_total": _money(
                expenses.aggregate(s=Sum("estimated_deductible_amount"))["s"]
            ),
            "assets_count": assets.count(),
            "assets_total": _money(
                assets.aggregate(s=Sum("total_cost"))["s"]
            ),
            "assets_deductible_total": _money(
                assets.aggregate(s=Sum("estimated_deductible_amount"))["s"]
            ),
        },
        "expenses_by_category": [
            {
                "category": row["category"],
                "total": _money(row["total"]),
                "deductible": _money(row["deductible"]),
            }
            for row in by_category
        ],
        "largest_expenses": [
            {
                "id": row["id"],
                "date": row["date"].isoformat() if row["date"] else None,
                "vendor": row["vendor"],
                "item": row["item"],
                "category": row["category"],
                "total_cost": _money(row["total_cost"]),
            }
            for row in largest
        ],
        "projects": projects,
        "content": content,
        "documentation": docs,
        "assets": asset_records,
    }


def year_summary_json(year: int) -> str:
    return json.dumps(year_summary(year), indent=2, sort_keys=False)


def _section(title, rows, line, empty, intro=()) -> list[str]:
    """One markdown section: a heading, a line per row, or the empty note."""

    body = [line(row) for row in rows] or [empty]
    return [f"## {title}", "", *intro, *body, ""]


def _totals_section(totals) -> list[str]:
    return [
        "## Totals",
        "",
        f"- Expenses: {totals['expenses_count']} records · "
        f"${totals['expenses_total']} total, "
        f"${totals['expenses_deductible_total']} estimated deductible",
        f"- Assets purchased this year: {totals['assets_count']} · "
        f"${totals['assets_total']} total, "
        f"${totals['assets_deductible_total']} estimated deductible",
        "",
    ]


def _category_line(row) -> str:
    return f"- **{row['category']}** ${row['total']} (${row['deductible']} deductible)"


def _expense_line(row) -> str:
    return (
        f"- {row['date'] or MISSING} · {row['vendor']} · {row['item']} "
        f"(`{row['category']}`) **${row['total_cost']}**"
    )


def _project_line(p) -> str:
    techs = ", ".join(p["technologies"]) if p["technologies"] else MISSING
    return f"- **{p['name']}** (`{p['slug']}`, {p['category']}, {p['status']}) · {techs}"


def _content_line(c) -> str:
    bits = [f"`{c['type']}`", c["status"]]
    if c["published_at"]:
        bits.append(f"published {c['published_at']}")
    if c["related_projects"]:
        bits.append("projects=" + ",".join(c["related_projects"]))
    if c["related_documentation"]:
        bits.append("docs=" + ",".join(c["related_documentation"]))
    return f"- **{c['title']}** (`{c['slug']}`) · {' · '.join(bits)}"


def _documentation_line(d) -> str:
    bits = [
        f"`{d['type']}`", d["environment"], d["status"],
        f"sensitivity={d['sensitivity']}",
    ]
    if d["obsidian_path"]:
        bits.append(f"obsidian=`{d['obsidian_path']}`")
    if d["github_path"]:
        bits.append(f"github=`{d['github_path']}`")
    if d["last_reviewed"]:
        bits.append(f"reviewed {d['last_reviewed']}")
    return f"- **{d['doc_id']}** · {d['title']} · {' · '.join(bits)}"


def _asset_line(a) -> str:
    return (
        f"- **{a['item_name']}** (`{a['slug']}`, {a['category']}, "
        f"{a['status']}) · ${a['total_cost']} on {a['purchase_date'] or MISSING} "
        f"({a['business_use_percentage']}% business, "
        f"${a['estimated_deductible_amount']} deductible)"
    )


RECORD_HOMES = [
    "## Where records live",
    "",
    "- Public website pages/writeups → ContentItem.published_url + related documentation",
    "- Runbooks & infra detail → DocumentationRecord.obsidian_path (Obsidian vault)",
    "- Source repos → Project.repository_url / DocumentationRecord.github_path",
    "- Receipts → Severino HQ only (sign-in required, never exported)",
    "",
]


def year_summary_markdown(year: int) -> str:
    data = year_summary(year)
    no_expenses = "_No expenses recorded._"
    lines = [
        f"# Severino HQ year summary {year}",
        "",
        f"_Generated: {data['generated_at']}_",
        "",
        f"> {data['disclaimer']}",
        "",
        *_totals_section(data["totals"]),
        *_section("Expenses by category", data["expenses_by_category"],
                  _category_line, no_expenses),
        *_section("Largest expenses", data["largest_expenses"],
                  _expense_line, no_expenses),
        *_section("Projects", data["projects"], _project_line,
                  "_No projects recorded._"),
        *_section("Writeups and pages", data["content"], _content_line,
                  "_No writeups or pages recorded._"),
        *_section("Documentation index", data["documentation"], _documentation_line,
                  "_No documentation records._",
                  intro=("_Pointers only. Doc bodies stay in the vault._", "")),
        *_section("Assets purchased this year", data["assets"], _asset_line,
                  "_No assets purchased this year._"),
        *RECORD_HOMES,
    ]
    return "\n".join(lines)
