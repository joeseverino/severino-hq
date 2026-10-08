from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import Http404
from django.urls import reverse, reverse_lazy
from django.utils.functional import cached_property
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from hq.domains.projects.github import github_repository
from hq.platform.application.documentation import related_documents
from hq.platform.application.entity_links import web_url
from hq.platform.application.pages import PageAction, PageMixin, record_trail
from hq.platform.application.projects import NotFoundError, hq_sections
from hq.platform.application.security import web_principal
from hq.platform.application.services import service_url_for
from hq.platform.application.tables import (
    TableColumn,
    TableFilter,
    TableListMixin,
    TableSort,
    TableToggle,
)
from hq.platform.application.timestamps import moment
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin

from .forms import ProjectForm
from .models import PROJECT_CATEGORY_CHOICES, Project

PROJECTS_TRAIL = ("Projects", reverse_lazy("projects:list"))


class ProjectListView(PageMixin, TableListMixin, ListView):
    model = Project
    template_name = "projects/project_list.html"
    paginate_by = 25
    page_title = "Projects"
    table_search_scope = "projects"
    table_selectable = True
    table_columns = (
        TableColumn("Name", "name"),
        TableColumn("Status", "status", css="key-col"),
        TableColumn("Technologies", "technologies_used"),
        TableColumn("Last push"),
        TableColumn("", css="row-actions"),
    )
    table_filters = (
        TableFilter("status", "Status", "status", Project.Status.choices),
        TableFilter("category", "Category", "category", PROJECT_CATEGORY_CHOICES),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently edited", ("archive_rank", "-updated_at")),
        TableSort(
            "updated_at", "Least recently edited", ("archive_rank", "updated_at")
        ),
        TableSort("name", "Name A–Z", ("archive_rank", "name")),
        TableSort("-name", "Name Z–A", ("archive_rank", "-name")),
        TableSort("status", "Status A–Z", ("archive_rank", "status")),
        TableSort("-status", "Status Z–A", ("archive_rank", "-status")),
        TableSort("category", "Category A–Z", ("archive_rank", "category")),
        TableSort("-category", "Category Z–A", ("archive_rank", "-category")),
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
        TableToggle("needs_output", "Active, nothing written yet"),
        TableToggle("no_content", "No writeup"),
        TableToggle("no_docs", "No documents"),
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

    def get_context_data(self, **kwargs):
        from hq.platform.application.github_estate import repository_for

        context = super().get_context_data(**kwargs)
        # GitHub's reading for the rows on this page: one stored reading,
        # whatever the page size.
        for project in context["object_list"]:
            repository = repository_for(project.repository_url)
            project.repository = repository
            project.pushed_at = moment(str(repository.pushed_at or "")) if repository is not None else None
        return context


class ProjectRefreshView(View):
    """Start a refresh of the project's outside metadata. The request answers
    once the job is recorded; the job reads GitHub and the published site."""

    def post(self, request, slug: str):
        from hq.domains.jobs.runner import JobConflict
        from hq.platform.application.asks import FAILED, Standing, answer, job_standing
        from hq.platform.application.projects import request_project_refresh

        back = reverse("projects:detail", args=[slug])
        try:
            job = request_project_refresh(
                slug, principal=web_principal(request.user), requested_by=request.user
            )
        except NotFoundError as exc:
            raise Http404(str(exc)) from exc
        except JobConflict:
            return answer(request, Standing(FAILED, "GitHub is already being read for a project."), fallback=back)
        return answer(
            request,
            job_standing(job),
            fallback=back,
            status_url=reverse("jobs:status", args=[job.pk]),
            message="Reading GitHub. This page updates when it finishes.",
        )


def refresh_ask(project):
    """The project's Read now control, standing as its last read does."""

    from hq.domains.jobs.models import Job
    from hq.platform.application.asks import Ask, Standing, job_standing
    from hq.platform.application.projects import REFRESH_JOB

    job = Job.objects.filter(kind=REFRESH_JOB, state__in=("queued", "running")).first()
    mine = job is not None and job.request.get("project") == project.slug
    return Ask(
        "Read now",
        reverse("projects:refresh", args=[project.slug]),
        standing=job_standing(job) if mine else Standing(),
        status_url=reverse("jobs:status", args=[job.pk]) if mine else "",
    )


class ProjectPage(PageMixin):
    """A page about one project, or a new one: its trail runs back to the list."""

    def get_page_trail(self):
        return record_trail(PROJECTS_TRAIL, getattr(self, "object", None), lambda project: project.name)


class ProjectDetailView(PageMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "project"
    queryset = Project.objects.prefetch_related(
        "content_items", "assets", "documentation_records", "expenses"
    )

    @cached_property
    def repository(self):
        """GitHub's stored reading of the project's repository, when there is one."""

        from hq.platform.application.github_estate import repository_for

        return repository_for(self.object.repository_url)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The reverse of the tie the service page makes. A project says where it
        # is published and HQ manages that name, so the two are one thing seen
        # from either side, and only one side led anywhere.
        context["service_url"] = service_url_for(self.object.public_url)
        context["github"] = self.repository
        context["documents"] = related_documents(
            self.object.documentation_records.all(), about=self.object
        )
        if context["github"] is not None:
            from hq.platform.application.github_posture import posture_of

            context["posture"] = posture_of(context["github"])
        from hq.platform.application.pages import PageBadge

        # Its state beside its name, as a service shows its reach; the category
        # leads the one line under it.
        context["page_badges"] = (PageBadge(self.object.get_status_display(), self.object.status),)
        return context

    def get_page_title(self):
        return self.object.name

    def get_page_lede(self):
        """The one line under the name, inside the head beside its actions."""

        from django.template.loader import render_to_string

        # The work moves where the code does: a push says when it last
        # changed better than the last edit of this record does.
        repository = self.repository
        return render_to_string(
            "projects/_project_meta.html",
            {
                "project": self.object,
                "service_url": service_url_for(self.object.public_url),
                "pushed_at": moment(str(repository.pushed_at or "")) if repository is not None else None,
                # The GitHub panel names the repository when it is read.
                "repository_url": "" if repository is not None else web_url(self.object.repository_url),
                "sections": hq_sections(self.object),
            },
        )

    def get_page_trail(self):
        return (PROJECTS_TRAIL,)

    def get_page_actions(self):
        project = self.object
        actions = []
        if github_repository(project.repository_url):
            actions.append(refresh_ask(project))
        actions += [
            PageAction("Edit", reverse("projects:edit", args=[project.slug])),
            PageAction(
                "Delete", reverse("projects:delete", args=[project.slug]), danger=True
            ),
        ]
        return tuple(actions)


class ProjectCreateView(ProjectPage, RecordFormMixin, CreateView):
    page_title = "New project"
    form_class = ProjectForm
    template_name = "projects/project_form.html"


class ProjectUpdateView(ProjectPage, RecordFormMixin, UpdateView):
    page_title = "Edit project"
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"


class ProjectDeleteView(ProjectPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete project?"
    model = Project
    template_name = "projects/project_confirm_delete.html"
    success_url = reverse_lazy("projects:list")
    context_object_name = "project"


class WatchingView(PageMixin, TemplateView):
    """Your GitHub profile and what you watch there. Yours: the account your
    sign-in claims, so nobody reads a login HQ was merely told about."""

    template_name = "projects/watching.html"
    page_title = "Starred repos"

    @property
    def login(self) -> str:
        from hq.platform.application.linked_accounts import GITHUB, linked_login

        return linked_login(self.request.user, GITHUB)

    def get_page_lede(self) -> str:
        return "What you star on GitHub, with each project's latest release and security advisories."

    def get_page_actions(self):
        if not self.login:
            return []
        from hq.platform.application.asks import Ask
        from hq.platform.application.github_profile import standing

        found = standing()
        return [
            Ask(
                "Read now",
                reverse("watching_refresh"),
                standing=found,
                status_url=_profile_status(found.since),
                refresh=WATCHING_REGION,
            ),
        ]

    def get_context_data(self, **kwargs):
        from hq.platform.application.github_profile import profile

        context = super().get_context_data(**kwargs)
        context["login"] = self.login
        context["profile"] = profile(self.login)
        context["app_repositories"] = self.app_repositories
        return context

    @property
    def app_repositories(self):
        """The repositories this account owns that HQ's GitHub App reads: a
        second proof the account is yours, from GitHub rather than the sign-in."""

        from hq.platform.application.github_estate import repositories

        owner = self.login.lower()
        from hq.domains.projects.models import Project
        from hq.platform.application.github_public import github_repository

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


# The part of the Watching page a finished read is shown in.
WATCHING_REGION = "[data-watching]"


def _profile_status(asked) -> str:
    from hq.platform.application.asks import read_status_url
    from hq.platform.application.github_profile import KIND

    return read_status_url((KIND,), asked) if asked is not None else ""


class WatchingRefreshView(View):
    """Ask the controller to read your GitHub profile now. Nothing is read here:
    the request answers once the ask is stored, and the page follows it."""

    def post(self, request):
        from hq.platform.application.asks import FAILED, Standing, answer, read_standing
        from hq.platform.application.github_profile import KIND, request_read
        from hq.platform.application.linked_accounts import GITHUB, linked_login
        from hq.platform.application.security import AuthorizationError

        login = linked_login(request.user, GITHUB)
        back = reverse("watching")
        if not login:
            return answer(request, Standing(FAILED, "Your sign-in names no GitHub account."), fallback=back)
        try:
            asked = request_read(login, principal=web_principal(request.user))
        except AuthorizationError:
            return answer(request, Standing(FAILED, "You may not read from GitHub."), fallback=back)
        return answer(
            request,
            read_standing((KIND,), asked),
            fallback=back,
            status_url=_profile_status(asked),
            message=f"Reading @{login} from GitHub. This page updates when it finishes.",
        )


class PostureView(PageMixin, TemplateView):
    """Every repository the GitHub App reads, against the standard it is held to.

    Led by what is not met, because that is what the page is for; a check met
    everywhere is one line at the end, and a repository is one row however
    many checks there are.
    """

    template_name = "projects/posture.html"
    page_title = "Repo checks"

    def get_page_lede(self) -> str:
        return "Each repository checked against your repository rules. Public ones have extra checks."

    def get_context_data(self, **kwargs):
        from hq.platform.application.github_posture import STANDARD, postures
        from hq.platform.application.standards import MET, UNMET

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
