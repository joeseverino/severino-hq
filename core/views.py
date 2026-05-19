"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from assets.models import Asset
from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

from .models import AuditLog

ZERO_MONEY = Decimal("0.00")
REVIEW_WINDOW_DAYS = 180


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)

        expense_ytd_summary = Expense.objects.filter(date__gte=year_start).aggregate(
            total=Sum("total_cost"),
            deductible=Sum("estimated_deductible_amount"),
            n=Count("id"),
        )

        review_cutoff = today - timedelta(days=REVIEW_WINDOW_DAYS)
        docs_needing_review = DocumentationRecord.objects.filter(
            Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=review_cutoff),
            status=DocumentationRecord.Status.ACTIVE,
        )

        docs_by_system = list(
            DocumentationRecord.objects
            .exclude(system_service="")
            .values_list("system_service")
            .annotate(n=Count("id"))
            .order_by("-n", "system_service")[:10]
        )
        env_counts = dict(
            DocumentationRecord.objects
            .values_list("environment")
            .annotate(n=Count("id"))
        )
        docs_by_environment = [
            (label, env_counts[value])
            for value, label in DocumentationRecord.Environment.choices
            if env_counts.get(value)
        ]

        ctx.update(
            active_project_count=Project.objects.filter(
                status=Project.Status.ACTIVE
            ).count(),
            active_projects=Project.objects.filter(
                status=Project.Status.ACTIVE
            ).order_by("-updated_at")[:4],
            draft_content=ContentItem.objects.filter(
                status=ContentItem.Status.DRAFT
            ).order_by("-updated_at")[:4],
            published_content_count=ContentItem.objects.filter(
                status=ContentItem.Status.PUBLISHED
            ).count(),
            recent_published=ContentItem.objects.filter(
                status=ContentItem.Status.PUBLISHED
            ).order_by("-published_at", "-updated_at")[:4],
            active_asset_count=Asset.objects.filter(
                status=Asset.Status.ACTIVE
            ).count(),
            expenses_ytd_total=expense_ytd_summary["total"] or ZERO_MONEY,
            expenses_ytd_count=expense_ytd_summary["n"] or 0,
            deductible_ytd_total=expense_ytd_summary["deductible"] or ZERO_MONEY,
            recent_receipts=Receipt.objects.order_by("-uploaded_at")[:4],
            docs_needing_review=docs_needing_review.order_by("last_reviewed")[:4],
            docs_needing_review_count=docs_needing_review.count(),
            docs_by_system=docs_by_system,
            docs_by_environment=docs_by_environment,
            recent_audit=AuditLog.objects.select_related("user")[:15],
            this_year=today.year,
        )
        return ctx


class AuditLogListView(LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user")
        q = self.request.GET.get("q", "").strip()
        action = self.request.GET.get("action", "").strip()
        if q:
            qs = qs.filter(object_repr__icontains=q) | qs.filter(
                message__icontains=q
            )
        if action:
            qs = qs.filter(action=action)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["actions"] = AuditLog.Action.choices
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_action"] = self.request.GET.get("action", "")
        return ctx
