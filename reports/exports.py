"""
Exports: CSV per entity, plus a year-summary in JSON and Markdown.

The Markdown export is designed to be AI-readable; the JSON export uses stable
internal IDs/slugs so a future MCP can reason about relationships.

No secrets, no sensitive doc bodies, no receipt file contents: by design.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from typing import Iterable

from django.db.models import Sum
from django.utils import timezone

from assets.models import Asset
from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from application.ui import MISSING


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


def expenses_csv(year: int | None = None) -> str:
    qs = Expense.objects.all()
    if year:
        qs = qs.filter(date__year=year)
    qs = qs.order_by("date")
    headers = [
        "id", "date", "vendor", "item", "category",
        "total_cost", "business_use_percentage", "estimated_deductible_amount",
        "payment_method", "business_purpose",
        "related_project", "related_asset", "related_content", "related_documentation",
        "notes",
    ]
    rows = (
        [
            e.id, e.date, e.vendor, e.item, e.category,
            e.total_cost, e.business_use_percentage, e.estimated_deductible_amount,
            e.payment_method, e.business_purpose,
            e.related_project.slug if e.related_project_id else "",
            e.related_asset.slug if e.related_asset_id else "",
            e.related_content.slug if e.related_content_id else "",
            e.related_documentation.doc_id if e.related_documentation_id else "",
            e.notes,
        ]
        for e in qs.select_related(
            "related_project", "related_asset", "related_content",
            "related_documentation",
        )
    )
    return _csv_response(headers, rows)


def assets_csv(year: int | None = None) -> str:
    qs = Asset.objects.all()
    if year:
        qs = qs.filter(purchase_date__year=year)
    qs = qs.order_by("-purchase_date")
    headers = [
        "id", "slug", "item_name", "vendor", "category",
        "purchase_date", "total_cost",
        "business_use_percentage", "estimated_deductible_amount",
        "payment_method", "serial_number", "warranty_date", "status", "notes",
    ]
    rows = (
        [
            a.id, a.slug, a.item_name, a.vendor, a.category,
            a.purchase_date, a.total_cost,
            a.business_use_percentage, a.estimated_deductible_amount,
            a.payment_method, a.serial_number, a.warranty_date, a.status, a.notes,
        ]
        for a in qs
    )
    return _csv_response(headers, rows)


def content_csv() -> str:
    qs = ContentItem.objects.all().order_by("-updated_at")
    headers = [
        "id", "slug", "title", "content_type", "status", "topic", "tags",
        "published_url", "published_at",
        "wordpress_post_id", "wordpress_slug",
    ]
    rows = (
        [
            c.id, c.slug, c.title, c.content_type, c.status, c.topic, c.tags,
            c.published_url, c.published_at,
            c.wordpress_post_id or "", c.wordpress_slug,
        ]
        for c in qs
    )
    return _csv_response(headers, rows)


def projects_csv() -> str:
    qs = Project.objects.all().order_by("-updated_at")
    headers = [
        "id", "slug", "name", "category", "status",
        "repository_url", "public_url", "technologies_used",
    ]
    rows = (
        [
            p.id, p.slug, p.name, p.category, p.status,
            p.repository_url, p.public_url, p.technologies_used,
        ]
        for p in qs
    )
    return _csv_response(headers, rows)


def documentation_csv() -> str:
    qs = DocumentationRecord.objects.all().order_by("doc_id")
    headers = [
        "doc_id", "title", "doc_type", "system_service", "environment",
        "status", "sensitivity",
        "obsidian_path", "github_path", "external_url", "last_reviewed",
    ]
    rows = (
        [
            d.doc_id, d.title, d.doc_type, d.system_service, d.environment,
            d.status, d.sensitivity,
            d.obsidian_path, d.github_path, d.external_url, d.last_reviewed,
        ]
        for d in qs
    )
    return _csv_response(headers, rows)


# ---------- Year summary -------------------------------------------------------

def _money(value) -> str:
    return f"{(value or Decimal('0.00')):.2f}"


def year_summary(year: int) -> dict:
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
            "related_projects": list(
                c.related_projects.values_list("slug", flat=True)
            ),
            "related_assets": list(
                c.related_assets.values_list("slug", flat=True)
            ),
            "related_documentation": list(
                c.related_documentation.values_list("doc_id", flat=True)
            ),
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
            "related_projects": list(
                d.related_projects.values_list("slug", flat=True)
            ),
            "related_assets": list(
                d.related_assets.values_list("slug", flat=True)
            ),
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
            "related_projects": list(
                a.related_projects.values_list("slug", flat=True)
            ),
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
        *_section("Content", data["content"], _content_line,
                  "_No content items recorded._"),
        *_section("Documentation index", data["documentation"], _documentation_line,
                  "_No documentation records._",
                  intro=("_Pointers only. Doc bodies stay in the vault._", "")),
        *_section("Assets purchased this year", data["assets"], _asset_line,
                  "_No assets purchased this year._"),
        *RECORD_HOMES,
    ]
    return "\n".join(lines)
