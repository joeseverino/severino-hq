from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
    View,
)

from application.documentation import (
    documentation_command_from_cleaned_data,
    save_documentation,
    sync_documentation,
)
from application.deletion import delete_documentation
from application.security import web_principal
from application.tables import TableFilter, TableListMixin, TableSort, TableToggle
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)

from .forms import DocumentationRecordForm, ManifestImportForm
from .importer import ManifestImportError
from .models import DocumentationRecord


class DocsListView(TableListMixin, LoginRequiredMixin, ListView):
    model = DocumentationRecord
    template_name = "docs_index/docs_list.html"
    context_object_name = "records"
    paginate_by = 25
    table_search_scope = "documentation"
    table_filters = (
        TableFilter("status", "Status", "status", DocumentationRecord.Status.choices),
        TableFilter(
            "environment",
            "Environment",
            "environment",
            DocumentationRecord.Environment.choices,
        ),
        TableFilter(
            "doc_type", "Type", "doc_type", DocumentationRecord.DocType.choices
        ),
        TableFilter(
            "sensitivity",
            "Sensitivity",
            "sensitivity",
            DocumentationRecord.Sensitivity.choices,
        ),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently updated", "-updated_at"),
        TableSort("doc_id", "Document ID", "doc_id"),
        TableSort("-doc_id", "Document ID reverse", "-doc_id"),
        TableSort("title", "Title A–Z", "title"),
        TableSort("-title", "Title Z–A", "-title"),
        TableSort("doc_type", "Type", "doc_type"),
        TableSort("-doc_type", "Type reverse", "-doc_type"),
        TableSort("environment", "Environment", "environment"),
        TableSort("-environment", "Environment reverse", "-environment"),
        TableSort("last_reviewed", "Oldest review", "last_reviewed"),
        TableSort("-last_reviewed", "Newest review", "-last_reviewed"),
        TableSort("status", "Status", "status"),
        TableSort("-status", "Status reverse", "-status"),
        TableSort("sensitivity", "Sensitivity", "sensitivity"),
        TableSort("-sensitivity", "Sensitivity reverse", "-sensitivity"),
        TableSort("updated_at", "Least recently updated", "updated_at"),
    )
    table_toggles = (TableToggle("needs_review", "Needs review"),)
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search IDs, titles, systems, paths, and notes…"

    def get_queryset(self):
        qs = DocumentationRecord.objects.all()
        q = self.request.GET.get("q", "").strip()
        doc_types = self.table_values("doc_type")
        needs_review = self.request.GET.get("needs_review", "").strip()

        # Writeups and pages live in the Content tab; hide them from the
        # default Docs view unless the user explicitly filtered for that
        # doc_type or searched for one.
        if not doc_types and not q:
            qs = qs.exclude(doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT)
        if needs_review:
            qs = qs.needing_review()
        return self.apply_table_query(qs)


class DocsDetailView(LoginRequiredMixin, DetailView):
    model = DocumentationRecord
    template_name = "docs_index/docs_detail.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    context_object_name = "record"
    queryset = DocumentationRecord.objects.prefetch_related(
        "related_projects",
        "related_assets",
        "related_expenses",
        "content_items",
    )


class DocsWrite:
    """What every documentation write shares, whichever direction it goes."""

    model = DocumentationRecord
    noun = "Doc record"
    result_key = "documentation"
    identity_attr = "doc_id"
    identity_kwarg = "current_doc_id"


class DocsCreateView(DocsWrite, ServiceCreateMixin, LoginRequiredMixin, CreateView):
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"
    service = staticmethod(save_documentation)
    command_from_cleaned_data = staticmethod(documentation_command_from_cleaned_data)


class DocsUpdateView(DocsWrite, ServiceUpdateMixin, LoginRequiredMixin, UpdateView):
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    service = staticmethod(save_documentation)
    command_from_cleaned_data = staticmethod(documentation_command_from_cleaned_data)


class DocsDeleteView(DocsWrite, ServiceDeleteMixin, LoginRequiredMixin, DeleteView):
    template_name = "docs_index/docs_confirm_delete.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    success_url = reverse_lazy("docs_index:list")
    context_object_name = "record"
    service = staticmethod(delete_documentation)


class ManifestImportView(LoginRequiredMixin, View):
    template_name = "docs_index/import.html"

    def get(self, request):
        return render(request, self.template_name, {"form": ManifestImportForm()})

    def post(self, request):
        form = ManifestImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        try:
            raw = form.cleaned_data["manifest_file"].read()
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            messages.error(request, f"Invalid JSON: {exc}")
            return render(request, self.template_name, {"form": form})
        try:
            result = sync_documentation(
                data,
                principal=web_principal(request.user),
                update_existing=form.cleaned_data["update_existing"],
            )
        except ManifestImportError as exc:
            messages.error(request, f"Import failed: {exc}")
            return render(request, self.template_name, {"form": form})
        if not result["ok"]:
            messages.error(request, f"Import failed validation: {result['problems']}")
            return render(request, self.template_name, {"form": form})
        stats = result["stats"]
        messages.success(
            request,
            (
                f"Manifest imported. Created {stats['created']}, "
                f"updated {stats['updated']}, skipped {stats['skipped']}."
            ),
        )
        return redirect("docs_index:list")
