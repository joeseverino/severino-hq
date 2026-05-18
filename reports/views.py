"""Reports dashboard + exports."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
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

        review_cutoff = timezone.localdate() - timedelta(days=180)
        docs_needing_review = DocumentationRecord.objects.filter(
            Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=review_cutoff),
            status=DocumentationRecord.Status.ACTIVE,
        )

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


class _BaseExportView(LoginRequiredMixin, View):
    def _serve(self, *, body: str, content_type: str, filename: str, export_label: str):
        record_event(
            action=AuditLog.Action.EXPORTED,
            type_label="Export",
            message=f"Generated export: {export_label}",
            metadata={"filename": filename, "bytes": len(body.encode("utf-8"))},
        )
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        return response


class ExpensesCSVView(_BaseExportView):
    def get(self, request):
        year_q = request.GET.get("year")
        year = int(year_q) if year_q and year_q.isdigit() else None
        suffix = f"-{year}" if year else ""
        return self._serve(
            body=exporters.expenses_csv(year),
            content_type="text/csv; charset=utf-8",
            filename=f"expenses{suffix}.csv",
            export_label=f"expenses{suffix}.csv",
        )


class AssetsCSVView(_BaseExportView):
    def get(self, request):
        year_q = request.GET.get("year")
        year = int(year_q) if year_q and year_q.isdigit() else None
        suffix = f"-{year}" if year else ""
        return self._serve(
            body=exporters.assets_csv(year),
            content_type="text/csv; charset=utf-8",
            filename=f"assets{suffix}.csv",
            export_label=f"assets{suffix}.csv",
        )


class ContentCSVView(_BaseExportView):
    def get(self, request):
        return self._serve(
            body=exporters.content_csv(),
            content_type="text/csv; charset=utf-8",
            filename="content.csv",
            export_label="content.csv",
        )


class ProjectsCSVView(_BaseExportView):
    def get(self, request):
        return self._serve(
            body=exporters.projects_csv(),
            content_type="text/csv; charset=utf-8",
            filename="projects.csv",
            export_label="projects.csv",
        )


class DocumentationCSVView(_BaseExportView):
    def get(self, request):
        return self._serve(
            body=exporters.documentation_csv(),
            content_type="text/csv; charset=utf-8",
            filename="documentation.csv",
            export_label="documentation.csv",
        )


class YearSummaryJSONView(_BaseExportView):
    def get(self, request):
        try:
            year = int(request.GET.get("year") or timezone.localdate().year)
        except ValueError:
            year = timezone.localdate().year
        return self._serve(
            body=exporters.year_summary_json(year),
            content_type="application/json; charset=utf-8",
            filename=f"year-summary-{year}.json",
            export_label=f"year-summary-{year}.json",
        )


class YearSummaryMarkdownView(_BaseExportView):
    def get(self, request):
        try:
            year = int(request.GET.get("year") or timezone.localdate().year)
        except ValueError:
            year = timezone.localdate().year
        return self._serve(
            body=exporters.year_summary_markdown(year),
            content_type="text/markdown; charset=utf-8",
            filename=f"year-summary-{year}.md",
            export_label=f"year-summary-{year}.md",
        )
