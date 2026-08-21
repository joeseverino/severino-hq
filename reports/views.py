"""Reports dashboard + exports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.generic import TemplateView, View

from assets.models import Asset
from content.models import ContentItem
from core.audit import record_event
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project

from . import exports as exporters


class ReportsView(LoginRequiredMixin, TemplateView):
    template_name = "reports/reports.html"

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
                {d.year for d in Expense.objects.dates("date", "year")}
                | {d.year for d in Asset.objects.dates("purchase_date", "year")}
                | {timezone.localdate().year},
                reverse=True,
            ),
            expenses_count=expense_summary["n"] or 0,
            expenses_total=expense_summary["total"] or zero,
            expenses_deductible=expense_summary["deductible"] or zero,
            assets_count=asset_summary["n"] or 0,
            assets_total=asset_summary["total"] or zero,
            expenses_by_category=list(
                expenses.values("category")
                .annotate(
                    total=Sum("total_cost"),
                    deductible=Sum("estimated_deductible_amount"),
                )
                .order_by("-total")
            ),
            largest_expenses=expenses.order_by("-total_cost")[:10],
            content_status_counts=list(
                ContentItem.objects.values("status")
                .annotate(n=Count("id"))
                .order_by("status")
            ),
            project_status_counts=list(
                Project.objects.values("status")
                .annotate(n=Count("id"))
                .order_by("status")
            ),
            docs_needing_review=docs_needing_review.order_by("last_reviewed")[:25],
            docs_needing_review_count=docs_needing_review.count(),
            recent_audit=AuditLog.objects.select_related("user")[:25],
        )
        return ctx


CSV = "text/csv; charset=utf-8"


@dataclass(frozen=True)
class Export:
    """One downloadable report, declared rather than written out.

    Every export did the same four things -- build a body, name a file, record
    that it was taken, and return it as an attachment -- and differed only in
    which builder and which name. Seven view classes stated those differences
    in prose; here they are data, and the four things happen once.
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


class ExportView(LoginRequiredMixin, View):
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
                # Answered rather than ignored. Silently exporting all time --
                # or this year -- for a request that named neither hands back a
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
