"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, TemplateView

from assets.models import Asset
from contacts.d1 import D1Error, query
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

        # Contact submissions live in Cloudflare D1, not HQ's database.
        # Fetch over the D1 HTTP API; degrade gracefully if it's unreachable.
        try:
            recent_contacts = query(
                "SELECT id, created_at, name, email, status "
                "FROM contact_submissions ORDER BY id DESC LIMIT 4"
            )
        except D1Error:
            recent_contacts = []

        draft_content_qs = ContentItem.objects.filter(
            status=ContentItem.Status.DRAFT
        )
        receipts_unlinked_count = Receipt.objects.filter(
            related_expense__isnull=True,
            related_asset__isnull=True,
        ).count()
        expenses_without_receipts_count = (
            Expense.objects.annotate(receipt_count=Count("receipts"))
            .filter(receipt_count=0)
            .count()
        )
        assets_missing_purchase_info_count = Asset.objects.filter(
            status=Asset.Status.ACTIVE
        ).filter(Q(purchase_date__isnull=True) | Q(total_cost=0)).count()
        content_without_docs_count = (
            ContentItem.objects.annotate(doc_count=Count("related_documentation"))
            .filter(doc_count=0)
            .count()
        )

        action_queue = self._action_queue(
            docs_needing_review_count=docs_needing_review.count(),
            draft_content_count=draft_content_qs.count(),
            receipts_unlinked_count=receipts_unlinked_count,
            expenses_without_receipts_count=expenses_without_receipts_count,
            assets_missing_purchase_info_count=assets_missing_purchase_info_count,
            content_without_docs_count=content_without_docs_count,
        )

        ctx.update(
            recent_contacts=recent_contacts,
            active_project_count=Project.objects.filter(
                status=Project.Status.ACTIVE
            ).count(),
            active_projects=Project.objects.filter(
                status=Project.Status.ACTIVE
            ).order_by("-updated_at")[:4],
            draft_content=draft_content_qs.order_by("-updated_at")[:4],
            draft_content_count=draft_content_qs.count(),
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
            action_queue=action_queue,
            action_queue_count=sum(1 for item in action_queue if item["count"]),
            receipts_unlinked_count=receipts_unlinked_count,
            expenses_without_receipts_count=expenses_without_receipts_count,
            assets_missing_purchase_info_count=assets_missing_purchase_info_count,
            content_without_docs_count=content_without_docs_count,
            this_year=today.year,
        )
        return ctx

    def _action_queue(
        self,
        *,
        docs_needing_review_count: int,
        draft_content_count: int,
        receipts_unlinked_count: int,
        expenses_without_receipts_count: int,
        assets_missing_purchase_info_count: int,
        content_without_docs_count: int,
    ) -> list[dict[str, object]]:
        return [
            {
                "label": "Docs need review",
                "count": docs_needing_review_count,
                "href": f"{reverse('docs_index:list')}?needs_review=1",
            },
            {
                "label": "Draft content",
                "count": draft_content_count,
                "href": f"{reverse('content:list')}?status=draft",
            },
            {
                "label": "Unlinked receipts",
                "count": receipts_unlinked_count,
                "href": reverse("receipts:list"),
            },
            {
                "label": "Expenses missing receipts",
                "count": expenses_without_receipts_count,
                "href": reverse("expenses:list"),
            },
            {
                "label": "Active assets missing purchase info",
                "count": assets_missing_purchase_info_count,
                "href": f"{reverse('assets:list')}?status=active",
            },
            {
                "label": "Content missing docs",
                "count": content_without_docs_count,
                "href": reverse("content:list"),
            },
        ]


class SearchView(LoginRequiredMixin, TemplateView):
    template_name = "search.html"
    result_limit = 8

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        results = self._search(q) if q else {}

        ctx.update(
            q=q,
            search_query=q,
            results=results,
            total=sum(len(items) for items in results.values()),
        )
        return ctx

    def _search(self, q: str) -> dict[str, object]:
        return {
            "Projects": Project.objects.filter(
                Q(name__icontains=q)
                | Q(slug__icontains=q)
                | Q(description__icontains=q)
                | Q(technologies_used__icontains=q)
            )[: self.result_limit],
            "Content": ContentItem.objects.filter(
                Q(title__icontains=q)
                | Q(slug__icontains=q)
                | Q(topic__icontains=q)
                | Q(tags__icontains=q)
            )[: self.result_limit],
            "Docs": DocumentationRecord.objects.filter(
                Q(doc_id__icontains=q)
                | Q(title__icontains=q)
                | Q(system_service__icontains=q)
                | Q(obsidian_path__icontains=q)
            )[: self.result_limit],
            "Assets": Asset.objects.filter(
                Q(item_name__icontains=q)
                | Q(slug__icontains=q)
                | Q(vendor__icontains=q)
                | Q(notes__icontains=q)
            )[: self.result_limit],
            "Expenses": Expense.objects.filter(
                Q(vendor__icontains=q)
                | Q(item__icontains=q)
                | Q(business_purpose__icontains=q)
                | Q(notes__icontains=q)
            )[: self.result_limit],
            "Receipts": Receipt.objects.filter(
                Q(vendor__icontains=q)
                | Q(original_filename__icontains=q)
                | Q(notes__icontains=q)
            )[: self.result_limit],
        }


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
