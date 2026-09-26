from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import redirect
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

from application.projects import (
    NotFoundError,
    project_command_from_cleaned_data,
    refresh_project,
    save_project,
)
from application.deletion import delete_project
from application.security import web_principal
from application.ui import counted
from application.pages import PageAction, PageMixin
from application.tables import (
    TableColumn,
    TableFilter,
    TableListMixin,
    TableSort,
    TableToggle,
)
from application.services import service_url_for
from application.writes import (
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
)
from .forms import ProjectForm
from .models import PROJECT_CATEGORY_CHOICES, Project


PROJECTS_TRAIL = ("Projects", reverse_lazy("projects:list"))


class ProjectListView(PageMixin, TableListMixin, LoginRequiredMixin, ListView):
    model = Project
    template_name = "projects/project_list.html"
    paginate_by = 25
    page_title = "Projects"
    table_search_scope = "projects"
    table_selectable = True
    table_columns = (
        TableColumn("Name", "name"),
        TableColumn("Category", "category"),
        TableColumn("Status", "status"),
        TableColumn("Tech", "technologies_used"),
        TableColumn("Updated", "updated_at"),
        TableColumn("", css="row-actions"),
    )
    table_filters = (
        TableFilter("status", "Status", "status", Project.Status.choices),
        TableFilter("category", "Category", "category", PROJECT_CATEGORY_CHOICES),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently updated", ("archive_rank", "-updated_at")),
        TableSort(
            "updated_at", "Least recently updated", ("archive_rank", "updated_at")
        ),
        TableSort("name", "Name A–Z", ("archive_rank", "name")),
        TableSort("-name", "Name Z–A", ("archive_rank", "-name")),
        TableSort("status", "Status", ("archive_rank", "status")),
        TableSort("-status", "Status reverse", ("archive_rank", "-status")),
        TableSort("category", "Category", ("archive_rank", "category")),
        TableSort("-category", "Category reverse", ("archive_rank", "-category")),
        TableSort(
            "technologies_used", "Technology A–Z", ("archive_rank", "technologies_used")
        ),
        TableSort(
            "-technologies_used",
            "Technology Z–A",
            ("archive_rank", "-technologies_used"),
        ),
    )
    table_toggles = (
        TableToggle("needs_output", "Needs output"),
        TableToggle("no_content", "Missing content"),
        TableToggle("no_docs", "Missing docs"),
    )
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search projects, technology, and notes…"

    def get_page_actions(self):
        return (PageAction("New project", reverse("projects:create"), primary=True),)

    def get_queryset(self):
        qs = Project.objects.all()
        needs_output = self.request.GET.get("needs_output", "").strip()
        no_content = self.request.GET.get("no_content", "").strip()
        no_docs = self.request.GET.get("no_docs", "").strip()
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
        qs = qs.annotate(
            archive_rank=Case(
                When(status=Project.Status.ARCHIVED, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        return self.apply_table_query(qs)


class ProjectRefreshView(LoginRequiredMixin, View):
    """Fetch metadata (like last push) from GitHub for a project."""

    def post(self, request, slug: str):
        try:
            result = refresh_project(slug, principal=web_principal(request.user))
        except NotFoundError as exc:
            raise Http404(str(exc)) from exc
        content = result["content"]
        if content and content["ok"]:
            messages.success(
                request,
                f"Synced {counted(content['total'], 'content item')} "
                f"({content['created']} new, {content['updated']} updated).",
            )
        elif content:
            messages.error(request, f"Content sync failed: {content['error']}")

        github = result["github"]
        if github and github["ok"]:
            messages.success(request, "Synced GitHub project metadata.")
        elif github:
            messages.warning(request, github["error"])
        return redirect("projects:detail", slug=slug)


class ProjectPage(PageMixin):
    """A page about one project, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        project = getattr(self, "object", None)
        if project is None:
            return (PROJECTS_TRAIL,)
        return (PROJECTS_TRAIL, (project.name, project.get_absolute_url()))


class ProjectDetailView(PageMixin, LoginRequiredMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "project"
    queryset = Project.objects.prefetch_related(
        "content_items", "assets", "documentation_records", "expenses"
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The reverse of the tie the service page makes. A project says where it
        # is published and HQ manages that name, so the two are one thing seen
        # from either side, and only one side led anywhere.
        context["service_url"] = service_url_for(self.object.public_url)
        return context

    def get_page_title(self):
        return self.object.name

    def get_page_lede(self):
        return format_html(
            '{} · <span class="pill pill-{}">{}</span>',
            self.object.get_category_display(),
            self.object.status,
            self.object.get_status_display(),
        )

    def get_page_trail(self):
        return (PROJECTS_TRAIL,)

    def get_page_actions(self):
        project = self.object
        actions = []
        if project.repository_url and "github.com" in project.repository_url:
            actions.append(
                PageAction(
                    "Refresh",
                    reverse("projects:refresh", args=[project.slug]),
                    method="post",
                )
            )
        actions += [
            PageAction("Edit", reverse("projects:edit", args=[project.slug])),
            PageAction(
                "Delete", reverse("projects:delete", args=[project.slug]), danger=True
            ),
        ]
        return tuple(actions)


class ProjectWrite:
    """What every project write shares, whichever direction it goes."""

    model = Project
    noun = "Project"
    result_key = "project"
    identity_attr = "slug"
    identity_kwarg = "current_slug"


class ProjectCreateView(
    ProjectWrite, ProjectPage, ServiceCreateMixin, LoginRequiredMixin, CreateView
):
    page_title = "New project"
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    service = staticmethod(save_project)
    command_from_cleaned_data = staticmethod(project_command_from_cleaned_data)


class ProjectUpdateView(
    ProjectWrite, ProjectPage, ServiceUpdateMixin, LoginRequiredMixin, UpdateView
):
    page_title = "Edit project"
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    service = staticmethod(save_project)
    command_from_cleaned_data = staticmethod(project_command_from_cleaned_data)


class ProjectDeleteView(
    ProjectWrite, ProjectPage, ServiceDeleteMixin, LoginRequiredMixin, DeleteView
):
    page_title = "Delete project?"
    template_name = "projects/project_confirm_delete.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    success_url = reverse_lazy("projects:list")
    context_object_name = "project"
    service = staticmethod(delete_project)
