import json
import urllib.request
import urllib.error
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect
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

from application.projects import project_command_from_cleaned_data, save_project
from application.deletion import DeleteCommand, delete_project
from application.security import web_principal
from core.audit import record_event
from core.models import AuditLog
from content.content_sync import ContentSyncError, sync_content_index
from .forms import ProjectForm
from .models import PROJECT_CATEGORY_CHOICES, Project


class ProjectListView(LoginRequiredMixin, ListView):
    model = Project
    template_name = "projects/project_list.html"
    context_object_name = "projects"
    paginate_by = 25

    def get_queryset(self):
        qs = Project.objects.all()
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        category = self.request.GET.get("category", "").strip()
        sort = self.request.GET.get("sort", "-updated_at")
        needs_output = self.request.GET.get("needs_output", "").strip()
        no_content = self.request.GET.get("no_content", "").strip()
        no_docs = self.request.GET.get("no_docs", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(technologies_used__icontains=q)
                | Q(notes__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)
        if category:
            qs = qs.filter(category=category)
        if needs_output or no_content or no_docs:
            qs = qs.annotate(
                content_count=Count("content_items", distinct=True),
                doc_count=Count("documentation_records", distinct=True),
            )
        if needs_output:
            qs = qs.filter(status=Project.Status.ACTIVE).filter(
                Q(content_count=0) | Q(doc_count=0)
            )
        if no_content:
            qs = qs.filter(content_count=0)
        if no_docs:
            qs = qs.filter(doc_count=0)
        if sort in {
            "name", "-name", "updated_at", "-updated_at",
            "status", "-status", "category", "-category",
        }:
            qs = qs.annotate(
                archive_rank=Case(
                    When(status=Project.Status.ARCHIVED, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            ).order_by("archive_rank", sort)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            q=self.request.GET.get("q", ""),
            selected_status=self.request.GET.get("status", ""),
            selected_category=self.request.GET.get("category", ""),
            sort=self.request.GET.get("sort", "-updated_at"),
            status_choices=Project.Status.choices,
            category_choices=PROJECT_CATEGORY_CHOICES,
            needs_output=self.request.GET.get("needs_output", ""),
            no_content=self.request.GET.get("no_content", ""),
            no_docs=self.request.GET.get("no_docs", ""),
        )
        return ctx


class ProjectRefreshView(LoginRequiredMixin, View):
    """Fetch metadata (like last push) from GitHub for a project."""

    def post(self, request, slug: str):
        project = get_object_or_404(Project, slug=slug)

        # Unified refresh: for the public-site project, also pull the live
        # published content index (mirrors what is actually on jseverino.com).
        if project.slug == getattr(settings, "CONTENT_INDEX_PROJECT_SLUG", ""):
            try:
                stats = sync_content_index()
                messages.success(
                    request,
                    f"Synced {stats['total']} content item(s) "
                    f"({stats['created']} new, {stats['updated']} updated).",
                )
                record_event(
                    action=AuditLog.Action.UPDATED,
                    obj=project,
                    type_label="Project",
                    message=(
                        f"Synced content index: {stats['total']} items "
                        f"({stats['created']} new, {stats['updated']} updated)"
                    ),
                )
            except ContentSyncError as exc:
                messages.error(request, f"Content sync failed: {exc}")

        if not project.repository_url or "github.com" not in project.repository_url:
            if project.slug != getattr(settings, "CONTENT_INDEX_PROJECT_SLUG", ""):
                messages.warning(request, "Project has no GitHub repository URL.")
            return redirect(project.get_absolute_url())

        # Extract owner/repo from URL: https://github.com/owner/repo
        parts = project.repository_url.strip("/").split("/")
        if len(parts) < 2:
            messages.error(request, "Invalid GitHub URL.")
            return redirect(project.get_absolute_url())

        repo_path = "/".join(parts[-2:])
        api_url = f"https://api.github.com/repos/{repo_path}"

        token = getattr(settings, "GITHUB_API_TOKEN", "")
        headers = {"Accept": "application/vnd.github.v3+json"}
        if token:
            headers["Authorization"] = f"token {token}"

        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                pushed_at = data.get("pushed_at")
                if pushed_at:
                    project.last_push_at = timezone.datetime.fromisoformat(
                        pushed_at.replace("Z", "+00:00")
                    )
                    project.save(update_fields=["last_push_at"])
                    messages.success(request, f"Synced GitHub metadata for {project.name}.")

                    record_event(
                        action=AuditLog.Action.UPDATED,
                        obj=project,
                        type_label="Project",
                        message=f"Synced GitHub metadata (Last Push: {pushed_at})",
                    )
                else:
                    messages.warning(request, "Could not find push metadata in GitHub response.")
        except urllib.error.HTTPError as e:
            messages.error(request, f"GitHub API error: {e.code}")
        except Exception as e:
            messages.error(request, f"Sync failed: {str(e)}")

        return redirect(project.get_absolute_url())


class ProjectDetailView(LoginRequiredMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "project"
    queryset = Project.objects.prefetch_related(
        "content_items", "assets", "documentation_records", "expenses"
    )


class ProjectCreateView(LoginRequiredMixin, CreateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"

    def form_valid(self, form):
        result = save_project(
            project_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
        )
        self.object = Project.objects.get(slug=result["project"]["slug"])
        messages.success(self.request, f"Project “{self.object}” created.")
        return redirect(self.object.get_absolute_url())


class ProjectUpdateView(LoginRequiredMixin, UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def form_valid(self, form):
        result = save_project(
            project_command_from_cleaned_data(form.cleaned_data),
            principal=web_principal(self.request.user),
            current_slug=self.get_object().slug,
        )
        self.object = Project.objects.get(slug=result["project"]["slug"])
        messages.success(self.request, f"Project “{self.object}” updated.")
        return redirect(self.object.get_absolute_url())


class ProjectDeleteView(LoginRequiredMixin, DeleteView):
    model = Project
    template_name = "projects/project_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("projects:list")
    context_object_name = "project"

    def form_valid(self, form):
        slug = self.get_object().slug
        result = delete_project(
            DeleteCommand(confirm=slug),
            principal=web_principal(self.request.user),
            current_slug=slug,
        )
        messages.success(self.request, f"Project “{result['deleted']['label']}” deleted.")
        return redirect(self.success_url)
