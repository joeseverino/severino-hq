from __future__ import annotations

import json

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
    View,
)

from hq.platform.application.documentation import (
    sync_documentation,
)
from hq.platform.application.security import web_principal
from hq.platform.application.pages import PageAction, PageMixin, page_context, record_trail
from hq.platform.application.tables import (
    TableColumn,
    TableFilter,
    TableListMixin,
    TableSort,
    TableToggle,
)
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin

from .forms import DocumentationRecordForm, ManifestImportForm
from .importer import ManifestImportError
from .models import DocumentationRecord


DOCS_TRAIL = ("Documentation index", reverse_lazy("docs_index:list"))


class DocsListView(PageMixin, TableListMixin, ListView):
    model = DocumentationRecord
    template_name = "docs_index/docs_list.html"
    paginate_by = 25
    page_title = "Documentation index"
    table_search_scope = "documentation"
    table_selectable = True
    table_columns = (
        TableColumn("Title", "title", css="key-col"),
        TableColumn("ID", "doc_id"),
        TableColumn("Type", "doc_type"),
        TableColumn("Environment", "environment"),
        TableColumn("Status", "status"),
        TableColumn("Sensitivity", "sensitivity"),
        TableColumn("Last reviewed", "last_reviewed"),
    )
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
        TableSort("doc_id", "ID A–Z", "doc_id"),
        TableSort("-doc_id", "ID Z–A", "-doc_id"),
        TableSort("title", "Title A–Z", "title"),
        TableSort("-title", "Title Z–A", "-title"),
        TableSort("doc_type", "Type A–Z", "doc_type"),
        TableSort("-doc_type", "Type Z–A", "-doc_type"),
        TableSort("environment", "Environment A–Z", "environment"),
        TableSort("-environment", "Environment Z–A", "-environment"),
        TableSort("last_reviewed", "Oldest review", "last_reviewed"),
        TableSort("-last_reviewed", "Newest review", "-last_reviewed"),
        TableSort("status", "Status A–Z", "status"),
        TableSort("-status", "Status Z–A", "-status"),
        TableSort("sensitivity", "Sensitivity A–Z", "sensitivity"),
        TableSort("-sensitivity", "Sensitivity Z–A", "-sensitivity"),
        TableSort("updated_at", "Least recently updated", "updated_at"),
    )
    table_toggles = (TableToggle("needs_review", "Due for review"),)
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search IDs, titles, systems, paths, and notes…"

    def get_page_actions(self):
        return (
            PageAction("Import manifest", reverse("docs_index:import")),
            PageAction("New document", reverse("docs_index:create"), primary=True),
        )

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


class DocsPage(PageMixin):
    """A page about one doc record, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(DOCS_TRAIL, getattr(self, "object", None), lambda record: record.doc_id)


class DocsDetailView(PageMixin, DetailView):
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

    def get_page_title(self):
        return self.object.title

    def get_page_lede(self):
        record = self.object
        return format_html(
            '<code>{}</code> · {} · {} · <span class="pill pill-{}">{}</span> · <span class="pill pill-{}">{}</span>',
            record.doc_id,
            record.get_doc_type_display(),
            record.get_environment_display(),
            record.status,
            record.get_status_display(),
            record.sensitivity,
            record.get_sensitivity_display(),
        )

    def get_page_trail(self):
        return (DOCS_TRAIL,)

    def get_page_actions(self):
        doc_id = self.object.doc_id
        return (
            PageAction("Edit", reverse("docs_index:edit", args=[doc_id])),
            PageAction("Delete", reverse("docs_index:delete", args=[doc_id]), danger=True),
        )


class DocsCreateView(DocsPage, RecordFormMixin, CreateView):
    page_title = "New document"
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"


class DocsUpdateView(DocsPage, RecordFormMixin, UpdateView):
    page_title = "Edit document"
    model = DocumentationRecord
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"


class DocsDeleteView(DocsPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete document?"
    model = DocumentationRecord
    template_name = "docs_index/docs_confirm_delete.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    success_url = reverse_lazy("docs_index:list")
    context_object_name = "record"


class ManifestImportView(View):
    template_name = "docs_index/import.html"

    def render_form(self, request, form):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                **page_context(
                    "Import documentation manifest",
                    "Upload a JSON list with one entry per vault document.",
                    trail=(DOCS_TRAIL,),
                ),
            },
        )

    def get(self, request):
        return self.render_form(request, ManifestImportForm())

    def post(self, request):
        form = ManifestImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_form(request, form)
        try:
            raw = form.cleaned_data["manifest_file"].read()
            data = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            messages.error(request, "This file is not UTF-8 text, so it cannot be read as JSON.")
            return self.render_form(request, form)
        except json.JSONDecodeError as exc:
            messages.error(request, f"This file is not valid JSON: {exc}")
            return self.render_form(request, form)
        try:
            result = sync_documentation(
                data,
                principal=web_principal(request.user),
                update_existing=form.cleaned_data["update_existing"],
            )
        except ManifestImportError as exc:
            messages.error(request, f"Nothing was imported: {exc}")
            return self.render_form(request, form)
        if not result["ok"]:
            messages.error(request, f"Nothing was imported. Fix these entries first: {result['problems']}")
            return self.render_form(request, form)
        stats = result["stats"]
        messages.success(
            request,
            (
                f"Manifest imported: {stats['created']} new, "
                f"{stats['updated']} changed, {stats['skipped']} unchanged."
            ),
        )
        return redirect("docs_index:list")
