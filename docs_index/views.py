from __future__ import annotations

import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
    View,
)

from core.audit import record_event
from core.models import AuditLog

from .forms import DocumentationRecordForm, ManifestImportForm
from .importer import ManifestImportError, import_manifest_data
from .models import DocumentationRecord


class DocsListView(LoginRequiredMixin, ListView):
    model = DocumentationRecord
    template_name = "docs_index/docs_list.html"
    context_object_name = "records"
    paginate_by = 25

    def get_queryset(self):
        qs = DocumentationRecord.objects.all()
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        env = self.request.GET.get("environment", "").strip()
        doc_type = self.request.GET.get("doc_type", "").strip()
        sensitivity = self.request.GET.get("sensitivity", "").strip()
        needs_review = self.request.GET.get("needs_review", "").strip()
        sort = self.request.GET.get("sort", "-updated_at")
        if q:
            qs = qs.filter(
                Q(doc_id__icontains=q)
                | Q(title__icontains=q)
                | Q(system_service__icontains=q)
                | Q(obsidian_path__icontains=q)
                | Q(notes__icontains=q)
            )
        for field, value in [
            ("status", status),
            ("environment", env),
            ("doc_type", doc_type),
            ("sensitivity", sensitivity),
        ]:
            if value:
                qs = qs.filter(**{field: value})

        # Writeups and pages live in the Content tab; hide them from the
        # default Docs view unless the user explicitly filtered for that
        # doc_type or searched for one.
        if not doc_type and not q:
            qs = qs.exclude(
                doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT
            )
        if needs_review:
            review_days = getattr(settings, "SEVERINO_DOC_REVIEW_INTERVAL_DAYS", 180)
            cutoff = timezone.localdate() - timedelta(days=review_days)
            qs = qs.filter(
                Q(last_reviewed__isnull=True) | Q(last_reviewed__lt=cutoff),
                status=DocumentationRecord.Status.ACTIVE,
            ).exclude(doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT)
        if sort in {
            "doc_id", "-doc_id", "title", "-title",
            "updated_at", "-updated_at", "last_reviewed", "-last_reviewed",
            "status", "-status",
        }:
            qs = qs.order_by(sort)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_status=self.request.GET.get("status", ""),
            selected_env=self.request.GET.get("environment", ""),
            selected_type=self.request.GET.get("doc_type", ""),
            selected_sensitivity=self.request.GET.get("sensitivity", ""),
            needs_review=self.request.GET.get("needs_review", ""),
            sort=self.request.GET.get("sort", "-updated_at"),
            status_choices=DocumentationRecord.Status.choices,
            env_choices=DocumentationRecord.Environment.choices,
            type_choices=DocumentationRecord.DocType.choices,
            sensitivity_choices=DocumentationRecord.Sensitivity.choices,
        )
        return ctx


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


class DocsCreateView(LoginRequiredMixin, CreateView):
    model = DocumentationRecord
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Doc record “{self.object}” created.")
        return response


class DocsUpdateView(LoginRequiredMixin, UpdateView):
    model = DocumentationRecord
    form_class = DocumentationRecordForm
    template_name = "docs_index/docs_form.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Doc record “{self.object}” updated.")
        return response


class DocsDeleteView(LoginRequiredMixin, DeleteView):
    model = DocumentationRecord
    template_name = "docs_index/docs_confirm_delete.html"
    slug_field = "doc_id"
    slug_url_kwarg = "doc_id"
    success_url = reverse_lazy("docs_index:list")
    context_object_name = "record"

    def form_valid(self, form):
        title = str(self.get_object())
        response = super().form_valid(form)
        messages.success(self.request, f"Doc record “{title}” deleted.")
        return response


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
            stats = import_manifest_data(
                data,
                update_existing=form.cleaned_data["update_existing"],
            )
        except ManifestImportError as exc:
            messages.error(request, f"Import failed: {exc}")
            return render(request, self.template_name, {"form": form})
        record_event(
            action=AuditLog.Action.IMPORTED,
            type_label="DocumentationRecord",
            message=(
                f"Imported docs manifest: created={stats['created']}, "
                f"updated={stats['updated']}, skipped={stats['skipped']}"
            ),
            metadata=stats,
        )
        messages.success(
            request,
            (
                f"Manifest imported. Created {stats['created']}, "
                f"updated {stats['updated']}, skipped {stats['skipped']}."
            ),
        )
        return redirect("docs_index:list")
