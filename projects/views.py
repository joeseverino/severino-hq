from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    TemplateView,
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
from projects.github import github_repository
from application.security import web_principal
from application.ui import counted, moment
from application.pages import PageAction, PageMixin, record_trail
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
                f"Synced {counted(content['total'], 'content item', 'content items')} "
                f"({content['created']} new, {content['updated']} updated).",
            )
        elif content:
            messages.error(request, f"Content sync failed: {content['error']}")

        _report_github(request, result)
        return redirect("projects:detail", slug=slug)


def _report_github(request, result) -> None:
    """Say what the refresh did about GitHub: the App's read asked for, or the
    public read it fell back to."""

    app = result.get("github_app")
    if app and app["ok"]:
        messages.success(request, app["message"])
        return
    if app:
        messages.warning(request, f"The GitHub App was not asked to read: {app['error']}")
    github = result["github"]
    if github and github["ok"]:
        messages.success(request, "Synced GitHub project metadata.")
    elif github:
        messages.warning(request, github["error"])


class ProjectPage(PageMixin):
    """A page about one project, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(PROJECTS_TRAIL, getattr(self, "object", None), lambda project: project.name)


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
        from application.github_estate import repository_for

        context["github"] = repository_for(self.object.repository_url)
        if context["github"] is not None:
            from application.github_posture import posture_of

            context["posture"] = posture_of(context["github"])
        from application.pages import PageBadge

        # Its state beside its name, as a service shows its reach; the category
        # leads the one line under it.
        context["page_badges"] = (PageBadge(self.object.get_status_display(), self.object.status),)
        return context

    def get_page_title(self):
        return self.object.name

    def get_page_lede(self):
        """The one line under the name, inside the head beside its actions."""

        from django.template.loader import render_to_string

        from application.github_estate import repository_for

        # The work moves where the code does: a push says when it last
        # changed better than the last edit of this record does.
        repository = repository_for(self.object.repository_url)
        return render_to_string(
            "projects/_project_meta.html",
            {
                "project": self.object,
                "service_url": service_url_for(self.object.public_url),
                "pushed_at": moment(str(repository.pushed_at or "")) if repository is not None else None,
            },
        )

    def get_page_trail(self):
        return (PROJECTS_TRAIL,)

    def get_page_actions(self):
        project = self.object
        actions = []
        if github_repository(project.repository_url):
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


class WatchingView(PageMixin, LoginRequiredMixin, TemplateView):
    """Your GitHub profile and what you watch there. Yours: the account your
    sign-in claims, so nobody reads a login HQ was merely told about."""

    template_name = "projects/watching.html"
    page_title = "Watching"

    @property
    def login(self) -> str:
        from application.linked_accounts import GITHUB, linked_login

        return linked_login(self.request.user, GITHUB)

    def get_page_lede(self) -> str:
        return "What you star on GitHub, with each project's latest release and security advisories."

    def get_page_actions(self) -> tuple[PageAction, ...]:
        if not self.login:
            return ()
        return (PageAction("Refresh", reverse("watching_refresh"), method="post"),)

    def get_context_data(self, **kwargs):
        from application.github_profile import profile

        context = super().get_context_data(**kwargs)
        context["login"] = self.login
        context["profile"] = profile(self.login)
        context["app_repositories"] = self.app_repositories
        return context

    @property
    def app_repositories(self):
        """The repositories this account owns that HQ's GitHub App reads: a
        second proof the account is yours, from GitHub rather than the sign-in."""

        from application.github_estate import repositories

        owner = self.login.lower()
        from projects.models import Project

        from application.github_public import github_repository

        # The HQ project each repository is, when one names it, so a row links there.
        projects = {
            "/".join(parts).lower(): project
            for project in Project.objects.exclude(repository_url="")
            if (parts := github_repository(project.repository_url))
        }
        return [
            {"repo": repo, "project": projects.get(repo.name.lower())}
            for repo in sorted(
                (repo for name, repo in repositories().items() if name.split("/", 1)[0].lower() == owner),
                key=lambda repo: repo.name,
            )
        ]


class WatchingRefreshView(LoginRequiredMixin, View):
    def post(self, request):
        from application.github_profile import refresh
        from application.github_public import GitHubReadError
        from application.linked_accounts import GITHUB, linked_login
        from application.security import AuthorizationError

        login = linked_login(request.user, GITHUB)
        if not login:
            messages.error(request, "Your sign-in names no GitHub account.")
            return redirect("watching")
        try:
            refresh(login, principal=web_principal(request.user), force=True)
        except AuthorizationError:
            messages.error(request, "You may not read from GitHub.")
        except GitHubReadError as exc:
            messages.warning(request, str(exc))
        else:
            messages.success(request, f"Read @{login} from GitHub.")
        return redirect("watching")


class PostureView(PageMixin, LoginRequiredMixin, TemplateView):
    """Every repository the GitHub App reads, against the standard it is held to.

    Led by what is not met, because that is what the page is for; a check met
    everywhere is one line at the end, and a repository is one row however
    many checks there are.
    """

    template_name = "projects/posture.html"
    page_title = "Posture"

    def get_page_lede(self) -> str:
        return "Every repository against your standard: the private one, and the public one on top of it."

    def get_context_data(self, **kwargs):
        from application.github_posture import STANDARD, postures
        from application.standards import MET, UNMET

        context = super().get_context_data(**kwargs)
        found = sorted(postures(), key=lambda item: (not item.unmet, item.subject.private, item.subject.name))
        projects = {
            "/".join(parts): project
            for project in Project.objects.exclude(repository_url="")
            if (parts := github_repository(project.repository_url))
        }
        context["repositories"] = [{"posture": item, "project": projects.get(item.subject.name)} for item in found]
        unmet = []
        everywhere = []
        for check in STANDARD:
            states = [(item.subject, item.state_of(check.id)) for item in found]
            held = [state for _, state in states if state]
            failing = [repo for repo, state in states if state == UNMET]
            if failing:
                unmet.append({"check": check, "repos": failing, "of": len(held)})
            elif held and all(state == MET for state in held):
                everywhere.append(check)
        unmet.sort(key=lambda item: not item["check"].serious)
        context["unmet"] = unmet
        context["everywhere"] = everywhere
        return context
