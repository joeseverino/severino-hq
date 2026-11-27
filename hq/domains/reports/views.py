"""Reports dashboard + exports."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, override

from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from hq.domains.assets.models import ASSET_CATEGORY_CHOICES, Asset
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import EXPENSE_CATEGORY_CHOICES, Expense
from hq.domains.projects.models import Project
from hq.platform.application.card_items import Counted, Kpi, ListRow, RowTitle
from hq.platform.application.entity_links import entity_link
from hq.platform.application.expenses import cost_totals, costs_by_category
from hq.platform.application.money import money
from hq.platform.application.pages import PageMixin
from hq.platform.application.projection import years_of
from hq.platform.application.reports import exported
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Cell, DataTable, TableColumn
from hq.platform.application.ui import counted
from hq.platform.application.url_query import with_query

from . import exports as exporters


def status_counts(model, list_url: str) -> list[dict]:
    """How many of ``model`` are in each status, each count opening that list."""

    labels = dict(model.Status.choices)
    counted = dict(model.objects.values_list("status").annotate(n=Count("id")))
    return [
        {"label": labels[status], "count": counted[status], "url": with_query(list_url, {"status": status})}
        for status in labels
        if counted.get(status)
    ]


def by_category(categories, choices, list_url: str, query: Mapping[str, Any] | None = None) -> list[dict]:
    """Each category's totals (``costs_by_category``), named as the forms name
    it and linked to the list filtered to it."""

    labels = dict(choices)
    return [
        {
            "label": labels.get(category, category),
            "url": with_query(list_url, {"category": category, **(query or {})}),
            "totals": totals,
        }
        for category, totals in categories
    ]


EXPENSE_CATEGORIES = (
    TableColumn("Category", cell=lambda row: Cell(row["label"], row["url"])),
    TableColumn("Total", kind="numeric", css="key-col", cell=lambda row: Cell(money(row["totals"].total))),
    TableColumn("Deductible (est.)", kind="numeric", cell=lambda row: Cell(money(row["totals"].deductible))),
)


class ReportsView(PageMixin, TemplateView):
    template_name = "reports/reports.html"
    page_title = "Reports"

    @override
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            year = int(self.request.GET.get("year") or timezone.localdate().year)
        except ValueError:
            year = timezone.localdate().year

        expenses = Expense.objects.filter(date__year=year)
        assets = Asset.objects.filter(purchase_date__year=year)

        spent = cost_totals(expenses)
        bought = cost_totals(assets)
        expenses_url = with_query(reverse("expenses:list"), {"year": year})

        ctx.update(
            figures=(
                Kpi(
                    f"Expenses {year}",
                    money(spent.total),
                    counted(spent.count, "expense"),
                    detail_url=expenses_url if spent.count else "",
                ),
                Kpi(f"Deductible (est.) {year}", money(spent.deductible)),
                Kpi(
                    Counted(bought.count, f"Asset purchased {year}", f"Assets purchased {year}"),
                    detail=f"{money(bought.total)} total" if bought else "",
                ),
            ),
            year=year,
            available_years=sorted(
                {*years_of(Expense, "date"), *years_of(Asset, "purchase_date"), timezone.localdate().year},
                reverse=True,
            ),
            expenses_by_category=DataTable.of(
                "Expenses by category",
                EXPENSE_CATEGORIES,
                by_category(
                    costs_by_category(expenses),
                    EXPENSE_CATEGORY_CHOICES,
                    reverse("expenses:list"),
                    {"year": year},
                ),
            ),
            largest_expenses=[
                ListRow(
                    entity_link("expense", str(expense.pk), label=expense.label),
                    detail=expense.get_category_display(),
                    meta=money(expense.total_cost),
                    at=expense.date,
                )
                for expense in expenses.order_by("-total_cost")[:10]
            ],
            assets_by_category=[
                ListRow(
                    RowTitle(row["label"], row["url"]),
                    detail=counted(row["totals"].count, "asset"),
                    meta=money(row["totals"].total) if row["totals"].total else "",
                )
                for row in by_category(
                    costs_by_category(Asset.objects.all()),
                    ASSET_CATEGORY_CHOICES,
                    reverse("assets:list"),
                )
            ],
            status_groups=[
                group
                for group in (
                    {"label": "Writeups and pages", "counts": status_counts(ContentItem, reverse("content:list"))},
                    {"label": "Projects", "counts": status_counts(Project, reverse("projects:list"))},
                )
                if group["counts"]
            ],
        )
        return ctx


CSV = "text/csv; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Export:
    """One downloadable report, declared rather than written out.

    Every export does the same four things (build a body, name a file, record
    that it was taken, and return it as an attachment) and differs only in
    which builder and which name, so those differences are data and the four
    things happen once.
    """

    name: str
    path: str
    build: Callable[..., str]
    content_type: str
    stem: str
    extension: str
    # "none": the report has no year. "optional": a year narrows it, and its
    # absence means all time. "required": a year always applies, defaulting to
    # this one.
    year: str = "none"

    def filename(self, year: int | None) -> str:
        suffix = f"-{year}" if year else ""
        return f"{self.stem}{suffix}.{self.extension}"


EXPORTS = (
    Export("expenses_csv", "export/expenses.csv", exporters.expenses_csv, CSV, "expenses", "csv", year="optional"),
    Export("assets_csv", "export/assets.csv", exporters.assets_csv, CSV, "assets", "csv", year="optional"),
    Export("content_csv", "export/content.csv", exporters.content_csv, CSV, "content", "csv"),
    Export("projects_csv", "export/projects.csv", exporters.projects_csv, CSV, "projects", "csv"),
    Export("documentation_csv", "export/documentation.csv", exporters.documentation_csv, CSV, "documentation", "csv"),
    Export(
        "year_summary_json",
        "export/year-summary.json",
        exporters.year_summary_json,
        "application/json; charset=utf-8",
        "year-summary",
        "json",
        year="required",
    ),
    Export(
        "year_summary_md",
        "export/year-summary.md",
        exporters.year_summary_markdown,
        "text/markdown; charset=utf-8",
        "year-summary",
        "md",
        year="required",
    ),
)


class ExportView(View):
    """Serve one declared export.

    Bound to its ``Export`` through ``as_view(export=...)``, so the URL table
    is the only place the set is enumerated.
    """

    export: Export = None

    def get(self, request):
        spec = self.export
        if spec.year == "none":
            year = None
        else:
            raw = request.GET.get("year", "").strip()
            if raw and not raw.isdigit():
                # Answered rather than ignored. Silently exporting all time
                # (or this year) for a request that named neither hands back a
                # document that is not the one asked for, and nothing says so.
                return HttpResponseBadRequest("year must be a four-digit year.")
            if raw:  # noqa: SIM108 - a nested conditional expression reads worse
                year = int(raw)
            else:
                year = timezone.localdate().year if spec.year == "required" else None

        body = spec.build() if spec.year == "none" else spec.build(year)
        filename = spec.filename(year)
        exported(filename, body, principal=web_principal(request.user))
        response = HttpResponse(body, content_type=spec.content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        return response
