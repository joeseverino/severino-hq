"""Dashboard + audit-log views."""

from __future__ import annotations

from datetime import date, datetime

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.urls import reverse
from django.views.generic import ListView, TemplateView

from application.dashboard import operating_snapshot
from application.tables import TableFilter, TableListMixin, TableSort
from assets.models import Asset
from contacts.d1 import (
    D1Error,
    get_recent_submissions,
    search_submissions,
)
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
        try:
            recent_contacts = get_recent_submissions(limit=4)
        except D1Error:
            recent_contacts = []
        snapshot = operating_snapshot()
        unread_contacts_count = next(
            item["count"]
            for item in snapshot["priority"]
            if item["code"] == "unread_contacts"
        )
        for project in snapshot["active_projects"]:
            project["updated_at"] = datetime.fromisoformat(project["updated_at"])
        for collection in (snapshot["draft_content"], snapshot["recent_published"]):
            for item in collection:
                item["updated_at"] = datetime.fromisoformat(item["updated_at"])
                if item["published_at"]:
                    item["published_at"] = date.fromisoformat(item["published_at"])
        for record in snapshot["docs_needing_review"]:
            if record["last_reviewed"]:
                record["last_reviewed"] = date.fromisoformat(record["last_reviewed"])
        for event in snapshot["recent_activity"]:
            event["created_at"] = datetime.fromisoformat(event["created_at"])
        routes = {
            "docs_review": f"{reverse('docs_index:list')}?needs_review=1",
            "draft_content": f"{reverse('content:list')}?status=draft",
            "unread_contacts": f"{reverse('contacts:list')}?status=unread",
            "projects_output": f"{reverse('projects:list')}?needs_output=1",
            "receipts_unlinked": f"{reverse('receipts:list')}?unlinked=1",
            "expenses_receipts": f"{reverse('expenses:list')}?no_receipts=1",
            "assets_purchase": f"{reverse('assets:list')}?missing_purchase=1",
            "content_docs": f"{reverse('content:list')}?no_docs=1",
        }
        action_queue = []
        for item in snapshot["priority"]:
            rendered = dict(item)
            rendered["href"] = (
                reverse(
                    "control_plane:detail",
                    kwargs={"key": item["resource_key"]},
                )
                if item["code"] == "infrastructure"
                else routes[item["code"]]
            )
            action_queue.append(rendered)

        # Live infra status is NOT computed here — HQ links out to Uptime Kuma
        # on the VPS rather than duplicating a status checker.
        external_links = [
            {"label": "Live status", "sub": "Uptime Kuma · VPS",
             "href": "https://status.jseverino.com"},
            {"label": "Health endpoint", "sub": "liveness",
             "href": "https://health.jseverino.com"},
            {"label": "Portainer", "sub": "containers",
             "href": "http://admin.homelab"},
            {"label": "Public site", "sub": "jseverino.com",
             "href": "https://jseverino.com"},
        ]

        ctx.update(
            recent_contacts=recent_contacts,
            unread_contacts_count=unread_contacts_count,
            active_project_count=snapshot["kpis"]["active_projects"],
            active_projects=snapshot["active_projects"],
            project_opportunities_count=snapshot["kpis"]["projects_needing_output"],
            external_links=external_links,
            draft_content=snapshot["draft_content"],
            draft_content_count=snapshot["kpis"]["draft_content"],
            published_content_count=snapshot["kpis"]["published_content"],
            recent_published=snapshot["recent_published"],
            expenses_ytd_total=snapshot["kpis"]["expenses_total"],
            expenses_ytd_count=snapshot["kpis"]["expenses_count"],
            deductible_ytd_total=snapshot["kpis"]["deductible_total"],
            docs_needing_review=snapshot["docs_needing_review"],
            docs_needing_review_count=snapshot["kpis"]["docs_needing_review"],
            recent_audit=snapshot["recent_activity"],
            action_queue=action_queue,
            action_queue_count=snapshot["priority_count"],
            action_queue_group_count=snapshot["priority_group_count"],
            this_year=snapshot["year"],
        )
        return ctx


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
        try:
            contacts = search_submissions(q, limit=self.result_limit)
        except D1Error:
            contacts = []

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
            "Contacts": contacts,
        }


class AuditLogListView(TableListMixin, LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "core/auditlog_list.html"
    context_object_name = "events"
    paginate_by = 50
    table_search_fields = ("object_repr", "message", "object_type", "object_id")
    table_filters = (
        TableFilter("action", "Action", "action", AuditLog.Action.choices),
    )
    table_sorts = (
        TableSort("-created_at", "Newest event", "-created_at"),
        TableSort("created_at", "Oldest event", "created_at"),
        TableSort("action", "Action", "action"),
        TableSort("object_type", "Object type", "object_type"),
    )
    table_default_sort = "-created_at"
    table_search_placeholder = "Search objects, IDs, and messages…"

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user")
        return self.apply_table_query(qs)
