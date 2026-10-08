"""Reports dashboard + exports."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from hq.platform.application.pages import PageMixin
from hq.platform.application.projection import years_of
from hq.platform.application.documentation import document_link
from hq.domains.assets.models import ASSET_CATEGORY_CHOICES, Asset
from hq.domains.content.models import ContentItem
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import EXPENSE_CATEGORY_CHOICES, Expense
from hq.domains.projects.models import Project

from . import exports as exporters


def status_counts(model, list_url: str) -> list[dict]:
    """How many of ``model`` are in each status, each count opening that list."""

    labels = dict(model.Status.choices)
    counted = dict(model.objects.values_list("status").annotate(n=Count("id")))
    return [
        {"label": labels[status], "count": counted[status], "url": f"{list_url}?status={status}"}
        for status in labels
        if counted.get(status)
    ]


def by_category(rows, choices, list_url: str, query: str = "") -> list[dict]:
    """Aggregate rows keyed by ``category``, named as the forms name them and
    linked to the list filtered to that category."""

    labels = dict(choices)
    return [
        {
            **row,
            "label": labels.get(row["category"], row["category"]),
            "url": f"{list_url}?category={row['category']}{query}",
        }
        for row in rows
    ]


class ReportsView(PageMixin, TemplateView):
    template_name = "reports/reports.html"
    page_title = "Reports"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        try:
            year = int(self.request.GET.get("year") or timezone.localdate().year)
        except ValueError:
            year = timezone.localdate().year

        expenses = Expense.objects.filter(date__year=year)
        assets = Asset.objects.filter(purchase_date__year=year)

        zero = Decimal("0.00")
        expense_summary = expenses.aggregate(
            n=Count("id"),
            total=Sum("total_cost"),
            deductible=Sum("estimated_deductible_amount"),
        )
        asset_summary = assets.aggregate(n=Count("id"), total=Sum("total_cost"))

        docs_needing_review = DocumentationRecord.objects.needing_review()

        ctx.update(
            year=year,
            available_years=sorted(
                {*years_of(Expense, "date"), *years_of(Asset, "purchase_date"), timezone.localdate().year},
                reverse=True,
            ),
            expenses_count=expense_summary["n"] or 0,
            expenses_total=expense_summary["total"] or zero,
            expenses_deductible=expense_summary["deductible"] or zero,
            assets_count=asset_summary["n"] or 0,
            assets_total=asset_summary["total"] or zero,
            expenses_url=f"{reverse('expenses:list')}?year={year}",
            expenses_by_category=by_category(
                expenses.values("category")
                .annotate(
                    total=Sum("total_cost"),
                    deductible=Sum("estimated_deductible_amount"),
                )
                .order_by("-total"),
                EXPENSE_CATEGORY_CHOICES,
                reverse("expenses:list"),
                f"&year={year}",
            ),
            largest_expenses=expenses.order_by("-total_cost")[:10],
            assets_by_category=by_category(
                Asset.objects.values("category")
                .annotate(n=Count("id"), total=Sum("total_cost"))
                .order_by("-total", "category"),
                ASSET_CATEGORY_CHOICES,
                reverse("assets:list"),
            ),
            status_groups=[
                group
                for group in (
                    {"label": "Writeups and pages", "counts": status_counts(ContentItem, reverse("content:list"))},
                    {"label": "Projects", "counts": status_counts(Project, reverse("projects:list"))},
                )
                if group["counts"]
            ],
            docs_needing_review=[
                (document_link(record), record.last_reviewed)
                for record in docs_needing_review.order_by("last_reviewed")[:25]
            ],
            docs_needing_review_count=docs_needing_review.count(),
            docs_needing_review_url=f"{reverse('docs_index:list')}?needs_review=1",
        )
        return ctx


CSV = "text/csv; charset=utf-8"


@dataclass(frozen=True)
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
    Export("expenses_csv", "export/expenses.csv", exporters.expenses_csv, CSV,
           "expenses", "csv", year="optional"),
    Export("assets_csv", "export/assets.csv", exporters.assets_csv, CSV,
           "assets", "csv", year="optional"),
    Export("content_csv", "export/content.csv", exporters.content_csv, CSV,
           "content", "csv"),
    Export("projects_csv", "export/projects.csv", exporters.projects_csv, CSV,
           "projects", "csv"),
    Export("documentation_csv", "export/documentation.csv",
           exporters.documentation_csv, CSV, "documentation", "csv"),
    Export("year_summary_json", "export/year-summary.json",
           exporters.year_summary_json, "application/json; charset=utf-8",
           "year-summary", "json", year="required"),
    Export("year_summary_md", "export/year-summary.md",
           exporters.year_summary_markdown, "text/markdown; charset=utf-8",
           "year-summary", "md", year="required"),
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
            if raw:
                year = int(raw)
            else:
                year = timezone.localdate().year if spec.year == "required" else None

        body = spec.build() if spec.year == "none" else spec.build(year)
        filename = spec.filename(year)
        record_event(
            action=AuditLog.Action.EXPORTED,
            type_label="Export",
            message=f"Generated export: {filename}",
            metadata={"filename": filename, "bytes": len(body.encode("utf-8"))},
        )
        response = HttpResponse(body, content_type=spec.content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        return response
