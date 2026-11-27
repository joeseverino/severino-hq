from dataclasses import replace
from typing import override

from django.urls import reverse, reverse_lazy
from django.utils.functional import cached_property
from django.utils.text import Truncator, slugify
from django.views.generic import (
    CreateView,
    DeleteView,
    TemplateView,
    UpdateView,
    View,
)
lazy from django.template.defaultfilters import linebreaksbr
lazy from django.template.loader import render_to_string

from hq.domains.projects.github import github_repository
from hq.platform.application import related
from hq.platform.application.action_links import capability_action_link
from hq.platform.application.capabilities import capability_words
from hq.platform.application.documentation import related_documents
from hq.platform.application.entity_links import entity_link, page_url, record_link, web_url
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import moment, when
from hq.platform.application.pages import PageAction, PageBadge, PageMixin, RecordPageMixin
from hq.platform.application.projects import (
    DEPENDENCY_UPDATES,
    REFRESH,
    hq_sections,
    projects_by_repository,
    repository_key,
)
from hq.platform.application.quoted import Quoted
from hq.platform.application.record_errors import Sentence, worded
from hq.platform.application.references import Kind
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Cell, DataTable, Row, StatusMark, TableColumn
from hq.platform.application.ui import counted, severity_tone
from hq.platform.application.where_it_runs import where_it_runs
from hq.platform.application.writes import RecordDeleteMixin, RecordFormMixin
lazy from hq.platform.application import standards
lazy from hq.platform.application.asks import Ask, AskMixin, Standing, read_standing, read_status_url
lazy from hq.platform.application.connection_setup import docs_url
lazy from hq.platform.application.dashboard import finding_rows
lazy from hq.platform.application.dependency_updates import KIND as RUN_KIND, RUN_NOW, asked_at, last_run
lazy from hq.platform.application.github_estate import check_state, repositories, repository_for
lazy from hq.platform.application.github_posture import STANDARD, posture_of, postures
lazy from hq.platform.application.github_profile import KIND, READ_NOW, profile, standing
lazy from hq.platform.application.linked_accounts import GITHUB, linked_login
lazy from hq.platform.application.outbound_work import ask
lazy from hq.platform.application.page_reads import Remedy
lazy from hq.platform.application.repository_findings import REPOSITORY_STANDARD, repositories_measured

from .models import PROJECT_CATEGORY_CHOICES, Project

PROJECTS_TRAIL = (("Projects", reverse_lazy("projects:list")),)


class ProjectListView(ListPageMixin, TemplateView):
    resource = "projects"
    table_empty = "No projects yet."
    template_name = "list_page.html"
    page_title = "Projects"
    table_selectable = True
    table_columns = (
        TableColumn("Name", "name", cell=lambda row: _named(row)),  # noqa: PLW0108 - _named is defined below the class
        TableColumn("Status", "status", css="key-col", cell=lambda row: Cell.state(row["state"])),
        TableColumn("Updates", cell=lambda row: Cell(lines=_updates(row))),
        TableColumn(
            "Technologies", "technologies_used", css="optional-col", cell=lambda row: _chips(row["technologies"])
        ),
        TableColumn("Repository", kind="status", cell=lambda row: Cell(lines=_repository(row))),
        TableColumn("Last active", kind="status", cell=lambda row: Cell(moment=row["last_active_at"], muted=True)),
    )
    table_search_placeholder = "Search projects, technology, and notes…"

    @override
    def get_page_actions(self):
        return (PageAction.command("project.create", primary=True),)

    @override
    def row(self, item):
        # GitHub's reading of the row's repository: one stored reading,
        # whatever the page size.
        repository = repository_for(item["repository_url"])
        link = entity_link(Kind.PROJECT, item["slug"])
        return {
            **item,
            "state": Project.Status(item["status"]),
            "repository": repository,
            "pushed_at": moment(item["last_push_at"]),
            "last_active_at": moment(item["last_active_at"]),
            "updates_link": replace(link, url=f"{link.url}#updates"),
        }

    @override
    def table_row(self, row, cells):
        return Row(cells, actions=tuple(filter(None, (PageAction.command("project.update", row["slug"]),))))


def _named(row) -> Cell:
    """A project by its name, its slug beneath where the name does not say it."""

    link = entity_link(Kind.PROJECT, row["slug"], label=row["name"])
    slug = (Cell(row["slug"], muted=True),) if row["slug"] != slugify(row["name"]) else ()
    return replace(Cell.of(link), lines=slug)


def _updates(row) -> tuple[Cell, ...]:
    """Whether its dependencies are kept up to date, the updates open, and its open alerts."""

    repository, found = row["repository"], []
    if row["repository_url"]:
        found.append(Cell("On", tone="good") if row["dependency_updates"] else Cell("Off", muted=True))
        if pulls := len(repository.update_pulls) if repository else 0:
            found.append(Cell.of(replace(row["updates_link"], label=counted(pulls, "open update", "open updates"))))
    if repository and repository.open_alerts:
        found.append(
            Cell(
                status=StatusMark(
                    counted(repository.open_alerts, "open alert", "open alerts"), tone="attention", pill=False
                )
            )
        )
    return tuple(found)


def _chips(names) -> Cell:
    """Each of a project's technologies, a chip."""

    chips = [Cell(status=StatusMark(name, chip=True)) for name in names]
    return replace(chips[0], parts=tuple(chips[1:])) if chips else Cell()


def _repository(row) -> tuple[Cell, ...]:
    """When its repository was last pushed to, and whether its checks fail."""

    if row["pushed_at"]:
        found = [Cell("Pushed", muted=True, parts=(Cell(moment=row["pushed_at"], muted=True),))]
    else:
        found = [Cell("Not read yet", muted=True)] if row["repository_url"] else []
    if row["repository"] and row["repository"].failing:
        failed = check_state("failure")
        found.append(Cell(f"Checks {failed.word.lower()}", tone=failed.tone))
    return tuple(found)


# The fact a run is shown in, fetched again when the run is handed off.
UPDATES_REGION = "#updates-state"


def run_now_ask(slug: str, principal, asked) -> Ask | None:
    """The page's Update dependencies control, standing as the ask made at
    ``asked`` does. None for a principal the capability does not permit."""

    if capability_action_link(RUN_NOW, principal=principal) is None:
        return None
    return Ask(
        capability_words(RUN_NOW),
        reverse("projects:updates_run", args=[slug]),
        standing=read_standing((RUN_KIND,), asked) if asked is not None else Standing(),
        status_url=read_status_url((RUN_KIND,), asked) if asked is not None else "",
        refresh=UPDATES_REGION,
    )


class ProjectUpdatesRunView(AskMixin, View):
    """Ask for a dependency update run now, which the controller's next pass hands off."""

    capability = RUN_NOW

    @override
    def back(self) -> str:
        return page_url(Kind.PROJECT, self.kwargs["slug"])


class ProjectPage(PageMixin):
    """A page about one project, or a new one: its trail runs back to the list."""

    model = Project
    page_trail = PROJECTS_TRAIL

    @override
    def trail_label(self, record):
        return record.name


class ProjectDetailView(RecordPageMixin, TemplateView):
    page_trail = PROJECTS_TRAIL
    template_name = "projects/project_detail.html"
    resource = "projects"
    context_object_name = "project"
    model = Project
    stored_related = ("content_items", "assets", "documentation_records", "expenses")

    @cached_property
    def repository(self):
        """GitHub's stored reading of the project's repository, when there is one."""

        return repository_for(self.record["repository_url"])

    @cached_property
    def run_asked(self):
        """When a run of the project's repository was asked for and not yet
        handed off. Only an opted-in repository's page asks."""

        if not self.record["dependency_updates"] or self.repository is None:
            return None
        return asked_at(self.record["repository_url"])

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["github"] = self.repository
        project = self.stored
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
            context["updates_run"] = last_run(self.record["repository_url"])
            context["run_asked"] = self.run_asked

        # Its state beside its name, as a service shows its reach; the category
        # leads the line of facts under it.
        state = Project.Status(self.record["status"])
        context["page_badges"] = (PageBadge(state.label, state.tone),)
        context["page_facts"] = self.facts()
        return context

    def facts(self):
        """Where the project lives and runs, the head's line of facts."""

        record, repository = self.record, self.repository
        return render_to_string(
            "projects/_project_meta.html",
            {
                "project": record,
                "category": dict(PROJECT_CATEGORY_CHOICES)[record["category"]],
                # The reverse of the tie the service page makes: where it is
                # published names the service, and the service what runs it.
                "runs_on": where_it_runs(record["public_url"]),
                "address": _written_as(record["public_url"]),
                "last_active_at": moment(record["last_active_at"]),
                # The GitHub panel names the repository when it is read.
                "repository_url": "" if repository is not None else web_url(record["repository_url"]),
                "repository": _written_as(record["repository_url"]),
                "sections": hq_sections(record["repository_url"]),
            },
        )

    @override
    def get_page_title(self):
        return self.record["name"]

    @override
    def get_page_lede(self):
        """What the project is, in its own words."""

        return linebreaksbr(self.record["description"])

    @override
    def get_page_actions(self):
        record = self.record
        actions = []
        if github_repository(record["repository_url"]):
            actions.append(ask(REFRESH, record["slug"]))
        if record["repository_url"]:
            switch = "off" if record["dependency_updates"] else "on"
            actions.append(PageAction.command(DEPENDENCY_UPDATES, record["slug"], variant=switch))
        if record["dependency_updates"] and self.repository is not None:
            run_now = run_now_ask(record["slug"], web_principal(self.request.user), self.run_asked)
            actions += [run_now] if run_now is not None else []
        actions += [
            PageAction.command("project.update", record["slug"]),
            PageAction.command("project.delete", record["slug"]),
        ]
        return tuple(actions)


class ProjectCreateView(ProjectPage, RecordFormMixin, CreateView):
    page_title = "New project"


class ProjectUpdateView(ProjectPage, RecordFormMixin, UpdateView):
    page_title = "Edit project"


class ProjectDeleteView(ProjectPage, RecordDeleteMixin, DeleteView):
    page_title = "Delete project?"
    template_name = "projects/project_confirm_delete.html"
    success_url = reverse_lazy("projects:list")
    context_object_name = "project"


def _published(at) -> Cell:
    """How long ago, quiet, as words that leave the cell's sort to its value."""

    return Cell(moment=moment(at), muted=True) if moment(at) else Cell()


def _release(repo) -> Cell:
    if not (release := repo["release"]):
        return Cell()
    return Cell(release["tag"], release["url"], external=True, role="prose", parts=(_published(release["published"]),))


def _advisories(repo) -> Cell:
    """Its two newest advisories, each its severity, its id, what it says and
    when, then how many more there are."""

    advisories = repo["advisories"]
    lines = [
        Cell(
            status=StatusMark(item.get("severity") or "advisory", tone=severity_tone(item.get("severity"))),
            parts=(
                Cell(item["id"], item["url"], external=True),
                Cell(Quoted(Truncator(item["summary"]).chars(90))),
                _published(item["published"]),
            ),
        )
        for item in advisories[:2]
    ]
    if len(advisories) > 2:
        more = Cell(f"{len(advisories) - 2} more", f"{repo['url']}/security/advisories", external=True)
        lines.append(Cell("and", muted=True, parts=(more,)))
    return Cell(lines=tuple(lines), role="prose")


def _starred(repo) -> Cell:
    description = Quoted(Truncator(repo["description"]).chars(110))
    return Cell(
        repo["name"],
        repo["url"],
        external=True,
        lines=(Cell(description, muted=True),) if description else (),
    )


_STARRED_COLUMNS = (
    TableColumn("Repository", cell=_starred),
    TableColumn("Latest release", kind="identifier", drop=2, cell=_release),
    TableColumn("Advisories", drop=1, cell=_advisories),
    TableColumn("Starred", kind="status", cell=lambda repo: Cell(moment=repo["starred"])),
)


class WatchingView(PageMixin, TemplateView):
    """Your GitHub profile and what you watch there. Yours: the account your
    sign-in claims, so nobody reads a login HQ was merely told about."""

    template_name = "projects/watching.html"
    page_title = "Starred repos"

    @cached_property
    def login(self) -> str:
        return linked_login(self.request.user, GITHUB)

    @override
    def get_page_lede(self) -> str:
        return "Your GitHub stars, with each project's latest release and advisories."

    @override
    def get_page_actions(self):
        if not self.login:
            return []

        found = standing()
        return [
            Ask(
                capability_words(READ_NOW),
                reverse("watching_refresh"),
                standing=found,
                status_url=read_status_url((KIND,), found.since) if found.since else "",
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
        if context["profile"]:
            context["starred"] = DataTable.of("Newest starred", _STARRED_COLUMNS, context["profile"]["watched"])
        context["app_repositories"] = self.app_repositories
        # Why the account is yours, on hover beside its name: the sign-in
        # names it, and the App installed on it says so from GitHub's side.
        context["account_tip"] = "\n".join(
            (
                "This is your GitHub account, according to your sign-in.",
                *(("HQ's GitHub App is installed on it.",) if self.app_repositories else ()),
            )
        )
        return context

    @cached_property
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


def _written_as(url: str) -> str:
    """An address as a reader names it: no scheme, no closing slash."""

    return str(url or "").removeprefix("https://").removeprefix("http://").rstrip("/")


class WatchingRefreshView(AskMixin, View):
    """Ask the controller to read your GitHub profile now."""

    capability = READ_NOW
    back_to = "watching"

    @cached_property
    def login(self) -> str:
        return linked_login(self.request.user, GITHUB)

    @override
    def cannot(self) -> Sentence:
        return worded("" if self.login else "Your sign-in names no GitHub account.")

    @override
    def payload(self):
        return {"login": self.login}


def _checked(row) -> Cell:
    """A repository's name, over whether it is public and its meter: a
    segment per check it is held to, so one check stands in the same place
    on every row."""

    posture, subject = row["posture"], row["posture"].subject
    if row["project"] is not None:
        named = Cell.of(replace(record_link(row["project"]), label=subject.short))
    else:
        named = Cell(subject.short, web_url(subject.url), external=True)
    shown = Cell("Private" if subject.private else "Public", muted=True)
    return replace(named, lines=(shown, Cell.meter(posture.results, posture.summary)))


def _access(subject) -> Cell:
    """Who can reach a repository: the people beside you, each deploy key and
    whether it can write and was used, and its Actions variables."""

    access = subject.access
    if not access:
        return Cell("Access not read", muted=True)
    others = len(access["collaborators"]) - 1
    reach = (
        Cell(
            status=StatusMark(
                counted(others, "other person has", "other people have"), "access", tone="serious", pill=False
            )
        )
        if others > 0
        else Cell("Only you")
    )
    parts = []
    for key in access["deploy_keys"]:
        parts.append(Cell(status=StatusMark(key["title"], icon="lock", pill=False, ink="context"), dot=True))
        if not key["read_only"]:
            parts.append(Cell(status=StatusMark("Can write", tone="serious", pill=False), dot=True))
        used = moment(key.get("last_used") or "")
        parts.append(Cell(f"used {when(used)}" if used else "never used", dot=True))
    if not access["deploy_keys"]:
        parts.append(Cell("no deploy keys", dot=True))
    if variables := subject.variables:
        named = counted(len(variables), "Actions variable", "Actions variables")
        parts.append(Cell(f"{named}: {', '.join(variables)}", dot=True))
    return replace(reach, muted=True, parts=tuple(replace(part, muted=True) for part in parts))


def _met(posture) -> Cell:
    tone = "serious" if posture.unmet else "good"
    mark = StatusMark(posture.met, "of", posture.measured, tone=tone, pill=False)
    return Cell(status=mark, sort=str(posture.met))


_POSTURE_COLUMNS = (
    TableColumn("Repository", kind="identifier", css="key-col", cell=_checked),
    TableColumn("Met", kind="numeric", drop=2, cell=lambda row: _met(row["posture"])),
    TableColumn("Access", drop=1, cell=lambda row: _access(row["posture"].subject)),
)


class PostureView(PageMixin, TemplateView):
    """Every repository the GitHub App reads, against the standard it is held to.

    Led by what is not met, because that is what the page is for; a check met
    everywhere is one line at the end, and a repository is one row however
    many checks there are.
    """

    template_name = "projects/posture.html"
    page_title = "Repo rules"
    page_reads = ("github.repository",)

    @override
    def get_page_lede(self) -> str:
        return "Each repository, checked against your rules."

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = sorted(postures(), key=lambda item: (not item.unmet, item.subject.private, item.subject.name))
        projects = projects_by_repository()
        context["repositories"] = [
            {"posture": item, "project": projects.get(repository_key(item.subject.name))} for item in found
        ]
        context["postures"] = DataTable.of("Repositories", _POSTURE_COLUMNS, context["repositories"])
        everywhere = []
        for check in STANDARD:
            held = [state for item in found if (state := item.state_of(check.id))]
            if held and all(state == standards.Standing.MET for state in held):
                everywhere.append(check)
        raised = REPOSITORY_STANDARD.raised(repositories_measured())
        context["unmet"] = finding_rows(
            "standards", "Standards", sorted(raised, key=lambda finding: finding.severity.rank)
        )
        context["everywhere"] = everywhere
        return context
