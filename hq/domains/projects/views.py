from typing import ClassVar, override

from django.db.models import Case, IntegerField, Value, When
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
lazy from django.template.loader import render_to_string
lazy from django.utils import timezone

from hq.domains.projects.github import github_repository
from hq.platform.application import related
from hq.platform.application.action_links import READ_NOW_CAPABILITY, capability_action_link, command_url
from hq.platform.application.capabilities import capability_registry, capability_words, execute_capability
from hq.platform.application.documentation import related_documents
from hq.platform.application.entity_links import web_url
from hq.platform.application.pages import PageAction, PageBadge, PageMixin, record_trail
from hq.platform.application.projects import (
    DEPENDENCY_UPDATES,
    REFRESH,
    hq_sections,
    projects_by_repository,
    repository_key,
)
from hq.platform.application.security import AuthorizationError, web_principal
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
lazy from hq.platform.application.asks import (
    FAILED,
    Ask,
    Standing,
    answer,
    read_standing,
    read_status_url,
)
lazy from hq.platform.application.connection_setup import docs_url
lazy from hq.platform.application.dependency_updates import KIND as RUN_KIND, RUN_NOW, asked_at, last_run
lazy from hq.platform.application.github_estate import repositories, repository_for
lazy from hq.platform.application.github_posture import STANDARD, posture_of, postures
lazy from hq.platform.application.github_profile import KIND, profile, request_read, standing
lazy from hq.platform.application.linked_accounts import GITHUB, linked_login
lazy from hq.platform.application.outbound_work import ask
lazy from hq.platform.application.page_reads import Remedy
lazy from hq.platform.application.standards import MET, UNMET

from .forms import ProjectForm
from .models import PROJECT_CATEGORY_CHOICES, Project

PROJECTS_TRAIL = ("Projects", reverse_lazy("projects:list"))


class ProjectListView(PageMixin, TableListMixin, ListView):
    table_empty = "No projects yet."
    model = Project
    template_name = "projects/project_list.html"
    paginate_by = 25
    page_title = "Projects"
    table_search_scope = "projects"
    table_selectable = True
    # A technology list runs to a sentence, so a phone keeps the name and its status.
    table_drop_narrow = 5
    table_columns = (
        TableColumn("Name", "name"),
        TableColumn("Status", "status", css="key-col"),
        TableColumn("Updates"),
        TableColumn("Technologies", "technologies_used"),
        TableColumn("Repository", kind="status"),
        TableColumn("Edited", "updated_at"),
        TableColumn("", css="row-actions"),
    )
    table_filters = (
        TableFilter("status", "Status", "status", Project.Status.choices),
        TableFilter("category", "Category", "category", PROJECT_CATEGORY_CHOICES),
    )
    table_sorts = (
        TableSort("-updated_at", "Recently edited", ("archive_rank", "-updated_at")),
        TableSort("updated_at", "Least recently edited", ("archive_rank", "updated_at")),
        TableSort("name", "Name A–Z", ("archive_rank", "name")),
        TableSort("-name", "Name Z–A", ("archive_rank", "-name")),
        TableSort("status", "Status A–Z", ("archive_rank", "status")),
        TableSort("-status", "Status Z–A", ("archive_rank", "-status")),
        TableSort("category", "Category A–Z", ("archive_rank", "category")),
        TableSort("-category", "Category Z–A", ("archive_rank", "-category")),
        TableSort("technologies_used", "Technology A–Z", ("archive_rank", "technologies_used")),
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
        TableToggle("updates_on", "Updates on"),
    )
    table_default_sort = "-updated_at"
    table_search_placeholder = "Search projects, technology, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction("New project", reverse("projects:create"), primary=True),)

    @override
    def get_queryset(self):
        qs = Project.objects.all()
        for toggle in self.table_toggles:
            if self.request.GET.get(toggle.name, "").strip():
                qs = getattr(qs, toggle.name)()
        qs = qs.annotate(
            archive_rank=Case(
                When(status=Project.Status.ARCHIVED, then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        return self.apply_table_query(qs)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # GitHub's reading for the rows on this page: one stored reading,
        # whatever the page size.
        for project in context["object_list"]:
            repository = repository_for(project.repository_url)
            project.repository = repository
            project.pushed_at = moment(str(repository.pushed_at or "")) if repository is not None else None
        return context


# The fact a run is shown in, fetched again when the run is handed off.
UPDATES_REGION = "#updates-state"


def run_now_ask(project, principal, asked) -> Ask | None:
    """The page's Update dependencies control, standing as the ask made at
    ``asked`` does. None for a principal the capability does not permit."""

    if capability_action_link(RUN_NOW, principal=principal) is None:
        return None
    return Ask(
        capability_words(RUN_NOW),
        reverse("projects:updates_run", args=[project.slug]),
        standing=read_standing((RUN_KIND,), asked) if asked is not None else Standing(),
        status_url=read_status_url((RUN_KIND,), asked) if asked is not None else "",
        refresh=UPDATES_REGION,
    )


class ProjectUpdatesRunView(View):
    """Ask for a dependency update run now. Nothing runs here: the request
    answers once the ask is stored, and the controller's next pass hands off."""

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request, slug: str):
        back = reverse("projects:detail", args=[slug])
        asked = timezone.now()
        result = execute_capability(RUN_NOW, {}, principal=web_principal(request.user))
        if not result.get("ok"):
            return answer(request, Standing(FAILED, result["error"]["message"]), fallback=back)
        return answer(
            request,
            read_standing((RUN_KIND,), asked),
            fallback=back,
            status_url=read_status_url((RUN_KIND,), asked),
            message=capability_registry()[RUN_NOW].requested(),
        )


class ProjectPage(PageMixin):
    """A page about one project, or a new one: its trail runs back to the list."""

    @override
    def get_page_trail(self):
        return record_trail(PROJECTS_TRAIL, getattr(self, "object", None), lambda project: project.name)


class ProjectDetailView(PageMixin, DetailView):
    model = Project
    template_name = "projects/project_detail.html"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    context_object_name = "project"
    queryset = Project.objects.prefetch_related("content_items", "assets", "documentation_records", "expenses")

    @cached_property
    def repository(self):
        """GitHub's stored reading of the project's repository, when there is one."""

        return repository_for(self.object.repository_url)

    @cached_property
    def run_asked(self):
        """When an update run was asked for and not yet handed off. Only an
        opted-in repository's page asks: the run covers every one of them."""

        return asked_at() if self.object.dependency_updates and self.repository is not None else None

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The reverse of the tie the service page makes. A project says where it
        # is published and HQ manages that name, so the two are one thing seen
        # from either side, and only one side led anywhere.
        context["service_url"] = service_url_for(self.object.public_url)
        context["github"] = self.repository
        project = self.object
        documents = related_documents(project.documentation_records.all(), about=project)
        context["related"] = related.related(
            related.writeups(project.content_items.all(), status=True),
            related.assets(project.assets.all()),
            related.expenses(project.expenses.all()),
            *documents.groups,
            related.links("Related projects", documents.projects),
        )
        if context["github"] is not None:
            context["posture"] = posture_of(context["github"])
            context["updates_run"] = last_run(project.repository_url)
            context["run_asked"] = self.run_asked

        # Its state beside its name, as a service shows its reach; the category
        # leads the one line under it.
        context["page_badges"] = (PageBadge(self.object.get_status_display(), self.object.status),)
        return context

    @override
    def get_page_title(self):
        return self.object.name

    @override
    def get_page_lede(self):
        """The one line under the name, inside the head beside its actions."""

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

    @override
    def get_page_trail(self):
        return (PROJECTS_TRAIL,)

    @override
    def get_page_actions(self):
        project = self.object
        actions = []
        if github_repository(project.repository_url):
            actions.append(ask(REFRESH, project.slug))
        if project.repository_url and (updates := command_url(DEPENDENCY_UPDATES, project.slug)):
            switch = "off" if project.dependency_updates else "on"
            actions.append(PageAction(capability_words(DEPENDENCY_UPDATES, switch), updates))
        if project.dependency_updates and self.repository is not None:
            run_now = run_now_ask(project, web_principal(self.request.user), self.run_asked)
            actions += [run_now] if run_now is not None else []
        actions += [
            PageAction("Edit", reverse("projects:edit", args=[project.slug])),
            PageAction("Delete", reverse("projects:delete", args=[project.slug]), danger=True),
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
        return linked_login(self.request.user, GITHUB)

    @override
    def get_page_lede(self) -> str:
        return "What you star on GitHub, with each project's latest release and security advisories."

    @override
    def get_page_actions(self):
        if not self.login:
            return []

        found = standing()
        return [
            Ask(
                capability_words(READ_NOW_CAPABILITY),
                reverse("watching_refresh"),
                standing=found,
                status_url=_profile_status(found.since),
                refresh=WATCHING_REGION,
            ),
        ]

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["login"] = self.login
        if not self.login:
            context["github_claim"] = Remedy(
                "Add your GitHub login to your sign-in", docs_url("Install.md", "your-github-account")
            )
        context["profile"] = profile(self.login)
        context["app_repositories"] = self.app_repositories
        return context

    @property
    def app_repositories(self):
        """The repositories this account owns that HQ's GitHub App reads: a
        second proof the account is yours, from GitHub rather than the sign-in."""

        owner = self.login.lower()
        # The HQ project each repository is, when one names it, so a row links there.
        projects = projects_by_repository()
        return [
            {"repo": repo, "project": projects.get(repository_key(repo.name))}
            for repo in sorted(
                (repo for name, repo in repositories().items() if name.split("/", 1)[0].lower() == owner),
                key=lambda repo: repo.name,
            )
        ]


# The part of the Watching page a finished read is shown in.
WATCHING_REGION = "[data-watching]"


def _profile_status(asked) -> str:
    return read_status_url((KIND,), asked) if asked is not None else ""


class WatchingRefreshView(View):
    """Ask the controller to read your GitHub profile now. Nothing is read here:
    the request answers once the ask is stored, and the page follows it."""

    def post(self, request):
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
            message=capability_registry()[READ_NOW_CAPABILITY].requested(f"@{login}"),
        )


class PostureView(PageMixin, TemplateView):
    """Every repository the GitHub App reads, against the standard it is held to.

    Led by what is not met, because that is what the page is for; a check met
    everywhere is one line at the end, and a repository is one row however
    many checks there are.
    """

    template_name = "projects/posture.html"
    page_title = "Repo checks"
    page_reads = ("github.repository",)

    @override
    def get_page_lede(self) -> str:
        return "Each repository checked against your repository rules. Public ones have extra checks."

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = sorted(postures(), key=lambda item: (not item.unmet, item.subject.private, item.subject.name))
        projects = projects_by_repository()
        context["repositories"] = [
            {"posture": item, "project": projects.get(repository_key(item.subject.name))} for item in found
        ]
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
