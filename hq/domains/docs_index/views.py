import json
from typing import override

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    TemplateView,
    UpdateView,
    View,
)

from hq.platform.application import related
from hq.platform.application.documentation import SYNC
from hq.platform.application.labels import dotted
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin, page_context
from hq.platform.application.refusal_codes import Code
from hq.platform.application.tables import TableColumn
from hq.platform.application.web_writes import execute_posted
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
from hq.platform.core.held_values import format_html
from hq.platform.core.templatetags.status_tags import status_pill
from hq.platform.core.templatetags.value_tags import breakable
from hq.platform.core.web_refusals import refused

from .forms import ManifestImportForm
from .models import DocStatus, DocumentationRecord

DOCS_TRAIL = (("Docs", reverse_lazy("docs_index:list")),)


class DocsListView(ListPageMixin, TemplateView):
    resource = "documentation"
    table_empty = "No documents yet."
    template_name = "docs_index/docs_list.html"
    page_title = "Docs"
    table_selectable = True
    # The ID sits beside a title of a dozen words, which it leaves the room.
    table_columns = (
        TableColumn("Title", "title", css="key-col"),
        TableColumn("ID", "doc_id"),
        TableColumn("Type", "doc_type"),
        TableColumn("Environment", "environment"),
        TableColumn("Status", "status"),
        TableColumn("Sensitivity", "sensitivity"),
        TableColumn("Last changed", "last_reviewed"),
    )
    table_search_placeholder = "Search IDs, titles, systems, paths, and notes…"

    @override
    def get_page_actions(self):
        return (
            PageAction.command(SYNC),
            PageAction.command("documentation.create", primary=True),
        )

    @override
    def row(self, item):
        record = DocumentationRecord
        return {
            **item,
            "state": record.Status(item["status"]),
            "doc_type_label": record.DocType(item["doc_type"]).label,
            "environment_label": record.Environment(item["environment"]).label,
            "sensitivity_label": record.Sensitivity(item["sensitivity"]).label,
        }


class DocsPage(PageMixin):
    """A page about one doc record, or a new one: its trail runs back to the list."""

    model = DocumentationRecord
    page_trail = DOCS_TRAIL

    @override
    def trail_label(self, record):
        return record.doc_id


class DocsDetailView(RecordPageMixin, TemplateView):
    resource = "documentation"
    context_object_name = "record"
    page_trail = DOCS_TRAIL
    template_name = "docs_index/docs_detail.html"
    model = DocumentationRecord
    stored_related = ("related_projects", "related_assets", "related_expenses", "content_items")

    @override
    def get_context_data(self, **kwargs):
        found = self.stored
        return super().get_context_data(**kwargs) | {
            "safe_for_ai_export": found.is_safe_for_ai_export,
            "related": related.related(
                related.projects(found.related_projects.all()),
                related.assets(found.related_assets.all()),
                related.expenses(found.related_expenses.all()),
                related.writeups(found.content_items.all()),
            ),
        }

    @override
    def get_page_title(self):
        return self.record["title"]

    @override
    def get_page_lede(self):
        record = self.record
        return format_html(
            "{} {} {} {}",
            format_html("<code>{}</code>", breakable(record["doc_id"], "·")),
            breakable(
                dotted(
                    DocumentationRecord.DocType(record["doc_type"]).label,
                    DocumentationRecord.Environment(record["environment"]).label,
                )
            ),
            breakable(status_pill(DocStatus(record["status"]))),
            breakable(status_pill(DocumentationRecord.Sensitivity(record["sensitivity"]).label, chip=True)),
        )

    @override
    def get_page_actions(self):
        doc_id = self.record["doc_id"]
        return (
            PageAction.command("documentation.update", doc_id),
            PageAction.command("documentation.delete", doc_id),
        )


class DocsCreateView(DocsPage, RecordFormMixin, CreateView):
    page_title = "New document"


class DocsUpdateView(DocsPage, RecordFormMixin, UpdateView):
    page_title = "Edit document"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"


class DocsDeleteView(DocsPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete document?"
    template_name = "docs_index/docs_confirm_delete.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    success_url = reverse_lazy("docs_index:list")
    context_object_name = "record"


class ManifestImportView(View):
    runs = (SYNC,)
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
                    trail=DOCS_TRAIL,
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
        result = execute_posted(
            request, SYNC, {"manifest": data, "update_existing": form.cleaned_data["update_existing"]}
        )
        if refusal := refused(request, result):
            if refusal.code == Code.INVALID_INPUT and refusal.details:
                messages.error(request, f"Fix these entries first: {refusal.details}")
            return self.render_form(request, form)
        return redirect("docs_index:list")
