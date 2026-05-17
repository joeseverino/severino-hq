"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from assets.models import Asset
from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

from .models import AuditLog


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        year_start = today.replace(month=1, day=1)

        expenses_ytd = Expense.objects.filter(date__gte=year_start)
        deductible_ytd = expenses_ytd.aggregate(
            total=Sum("estimated_deductible_amount")
        )["total"] or Decimal("0.00")

        review_cutoff = today - timedelta(days=180)
        docs_needing_review = DocumentationRecord.objects.filter(
            status=DocumentationRecord.Status.ACTIVE,
        ).filter(
            models_last_reviewed_or_null(review_cutoff)
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
            .values_list("environment", "n")
        )
        docs_by_environment = [
            (label, env_counts.get(value, 0))
            for value, label in DocumentationRecord.Environment.choices
            if env_counts.get(value, 0) > 0
        ]

        ctx.update(
            active_projects=Project.objects.filter(
                status=Project.Status.ACTIVE
            ).order_by("-updated_at")[:4],
            draft_content=ContentItem.objects.filter(
                status__in=[
                    ContentItem.Status.IDEA,
                    ContentItem.Status.RESEARCHING,
                    ContentItem.Status.DRAFTING,
                    ContentItem.Status.EDITING,
                ]
            ).order_by("-updated_at")[:4],
            published_content_count=ContentItem.objects.filter(
                status=ContentItem.Status.PUBLISHED
            ).count(),
            recent_published=ContentItem.objects.filter(
                status=ContentItem.Status.PUBLISHED
            ).order_by("-published_at", "-updated_at")[:4],
            active_assets=Asset.objects.filter(
                status=Asset.Status.ACTIVE
            ).order_by("-purchase_date")[:4],
            active_asset_count=Asset.objects.filter(
                status=Asset.Status.ACTIVE
            ).count(),
            expenses_ytd_total=expenses_ytd.aggregate(total=Sum("total_cost"))["total"]
            or Decimal("0.00"),
            expenses_ytd_count=expenses_ytd.count(),
            deductible_ytd_total=deductible_ytd,
            recent_receipts=Receipt.objects.order_by("-uploaded_at")[:4],
            docs_needing_review=docs_needing_review.order_by("last_reviewed")[:4],
            docs_needing_review_count=docs_needing_review.count(),
            docs_by_system=docs_by_system,
            docs_by_environment=docs_by_environment,
            recent_audit=AuditLog.objects.select_related("user")[:15],
            project_status_counts=_status_counts(
                Project, Project.Status.choices
            ),
            content_status_counts=_status_counts(
                ContentItem, ContentItem.Status.choices
            ),
            this_year=today.year,
        )
        return ctx


def models_last_reviewed_or_null(cutoff: date):
    from django.db.models import Q

    return Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=cutoff)


def _status_counts(model, choices):
    counts = dict(
        model.objects.values_list("status").annotate(n=Count("id")).values_list(
            "status", "n"
        )
    )
    return [(label, counts.get(value, 0)) for value, label in choices]


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
