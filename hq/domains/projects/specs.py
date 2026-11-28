"""The projects' commands and resource, declared on its domain in application/domains.py."""

from typing import Annotated, NotRequired, TypedDict

from django.db.models import Exists, OuterRef, Q

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.application import dependency_updates, projects, registry_import
from hq.platform.application.answer_fields import (
    LinkedContent,
    LinkedDocs,
    LinkedExpenses,
    PageUrl,
    RecordSlug,
    UpdatedAt,
)
from hq.platform.application.contracts import Effect
from hq.platform.application.describe import Describe
from hq.platform.application.domains import records_of
from hq.platform.application.integration_specs import (
    CapabilitySpec,
    DetailRead,
    ListFilter,
    ListRead,
    ListSort,
    ListToggle,
    ResourceSpec,
)
from hq.platform.application.record_fields import Notes, PublishedUrl
from hq.platform.application.resources import BoundedQuery, SearchText
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import PROJECT_CATEGORY_CHOICES, Project


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "hq.import",
            "Import projects and assets from one file. Nothing is saved unless every record in it is valid.",
            Effect.REMOTE_WRITE,
            # The capabilities of the saves it runs, both of them.
            registry_import.REQUIRED_CAPABILITIES,
            registry_import.HQImportCommand,
            registry_import.execute_hq_import,
            subject_resource="projects",
            execution_notes=(
                "Validate every record before writing any.",
                "Create or update each record as its kind's create and update capabilities do, in one transaction.",
                "Keep a stored derived field that differs, and report it.",
                "Record one audit event per record and one for the import.",
            ),
            label="Import projects and assets",
        ),
        CapabilitySpec(
            projects.DEPENDENCY_UPDATES,
            "Turn HQ's dependency update pull requests on or off for a project's repository, from the next run.",
            Effect.REMOTE_WRITE,
            records_of("projects").write,
            projects.DependencyUpdatesCommand,
            projects.set_dependency_updates,
            "slug",
            "projects",
            target_label="Project",
            target_help="The project whose repository to update.",
            execution_notes=(
                "Refuse a project that names no repository.",
                "Turn updates on only while the repository passes its vulnerability-alerts check.",
                "Record the switch and who set it; the next update run reads it.",
            ),
            target_initial_fields=("dependency_updates",),
            label="Turn dependency updates on or off",
            variants=(("on", "Turn on updates"), ("off", "Turn off updates")),
            wants="turn dependency updates on or off for {target}",
        ),
        CapabilitySpec(
            dependency_updates.RUN_NOW,
            "Update dependencies in every opted-in repository.",
            # The controller's run opens pull requests on GitHub.
            Effect.INFRASTRUCTURE_CHANGE,
            # The ask is a controller read, which needs infrastructure as well.
            (records_of("projects").write, Capability.MANAGE_INFRASTRUCTURE),
            dependency_updates.DependencyRunCommand,
            dependency_updates.request_run,
            subject_resource="projects",
            execution_notes=(
                "Refuse when no project is opted in.",
                "Ask the controller for a run and wake it; it hands one token per account to the host.",
                "The run's outcome comes back as the github.dependency_run reading.",
            ),
            label="Update dependencies",
        ),
    )


class ProjectQuery(BoundedQuery):
    query: SearchText = None


def _without(relation: str) -> Q:
    """The projects no record of ``relation`` names."""

    through = getattr(Project, relation).through
    return Q(~Exists(through.objects.filter(project=OuterRef("pk"))))


def _written(*kept: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Four projects, each written about differently, split into ``kept`` and the rest."""

    _bare, half, whole, idle = (
        Project.objects.create(name=name, slug=name.lower(), status=status)
        for name, status in (
            ("Bare", Project.Status.ACTIVE),
            ("Half", Project.Status.ACTIVE),
            ("Whole", Project.Status.ACTIVE),
            ("Idle", Project.Status.PAUSED),
        )
    )
    ContentItem.objects.create(title="Example writeup", slug="example-writeup").related_projects.add(half, whole)
    DocumentationRecord.objects.create(doc_id="rb-example", title="Example runbook").related_projects.add(whole, idle)
    return kept, tuple({"bare", "half", "whole", "idle"} - {*kept})


def _updating() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A project the update run covers, kept, and one it does not, left out."""

    Project.objects.create(name="Updated", slug="updated", dependency_updates=True)
    Project.objects.create(name="Left", slug="left")
    return ("updated",), ("left",)


def _archived_last() -> tuple[str, str]:
    """An archived project named ahead of an active one still comes after it."""

    Project.objects.create(name="ZZZ active", slug="zzz-active", status=Project.Status.ACTIVE)
    Project.objects.create(name="AAA archived", slug="aaa-archived", status=Project.Status.ARCHIVED)
    return "zzz-active", "aaa-archived"


NO_CONTENT = ListToggle(
    "no_content",
    "No writeup",
    "True for only the projects no writeup or page names.",
    _without("content_items"),
    lambda: _written("bare", "idle"),
)
NO_DOCS = ListToggle(
    "no_docs",
    "No documents",
    "True for only the projects no document names.",
    _without("documentation_records"),
    lambda: _written("bare", "half"),
)
NEEDS_OUTPUT = ListToggle(
    "needs_output",
    "Active, nothing written yet",
    "True for only the active projects still missing a writeup or a document.",
    Q(status=Project.Status.ACTIVE) & (NO_CONTENT.keeps | NO_DOCS.keeps),
    lambda: _written("bare", "half"),
)
UPDATES_ON = ListToggle(
    "updates_on",
    "Updates on",
    "True for only the projects HQ's dependency update run covers.",
    Q(dependency_updates=True),
    _updating,
)
FILTERS = (
    ListFilter("status", "Status", Project.Status.choices),
    ListFilter("category", "Category", PROJECT_CATEGORY_CHOICES),
)

# Each order the list is read in, the first where none is asked. Archived
# projects come last in every one, ranked so by ``list_projects``.
SORTS = (
    ListSort("name", "Name A–Z", ("archive_rank", "name"), _archived_last),
    *(
        ListSort(sort.name, sort.label, ("archive_rank", *sort.ordering))
        for sort in (
            ListSort("-name", "Name Z–A"),
            *ListSort.both("status", "Status"),
            *ListSort.both("category", "Category"),
            *ListSort.both("technologies_used", "Technology"),
        )
    ),
)


class UpdateRun(TypedDict):
    """How the latest dependency update run that covered the repository went."""

    outcome: Annotated[
        str,
        Describe("How it ended: `ok`, `failed` or `timeout`, or `handed_off` while it still runs."),
    ]
    started_at: Annotated[str | None, Describe("When it started, an ISO 8601 date-time.")]
    finished_at: Annotated[str | None, Describe("When it ended, an ISO 8601 date-time; null while it still runs.")]


class Updates(TypedDict):
    """What HQ's update run has open in the repository, and its latest run, null before any; a count it
    could not read is null."""

    open_pulls: Annotated[int | None, Describe("How many pull requests the update run has open in the repository.")]
    oldest_pull_days: Annotated[
        int | None, Describe("How many days the oldest of them has been open; null when none is.")
    ]
    unmatched_alerts: Annotated[
        int | None, Describe("How many open Dependabot alerts no open update pull request answers.")
    ]
    last_run: UpdateRun | None


class ProjectRow(TypedDict):
    """One project; `updates` only where its repository is on GitHub."""

    slug: RecordSlug
    url: PageUrl
    name: projects.ProjectName
    category: projects.ProjectCategory
    status: projects.ProjectStatus
    description: projects.ProjectDescription
    technologies: Annotated[list[str], Describe("The technologies it uses, in the order they were written.")]
    repository_url: projects.RepositoryUrl
    public_url: PublishedUrl
    last_push_at: Annotated[
        str | None,
        Describe("When its repository was last pushed to, an ISO 8601 date-time; null until HQ has read it."),
    ]
    dependency_updates: projects.DependencyUpdates
    deployment_notes: projects.DeploymentNotes
    security_notes: projects.SecurityNotes
    notes: Notes
    updated_at: UpdatedAt
    updates: NotRequired[Updates]


class ProjectLinks(TypedDict):
    """The records a project is linked to."""

    assets: Annotated[list[str], Describe("The slugs of the assets this project uses, sorted.")]
    content: LinkedContent
    documentation: LinkedDocs
    expense_ids: LinkedExpenses


class ProjectRecord(ProjectRow):
    """One project with the records it is linked to."""

    relationships: ProjectLinks


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "projects",
            "Projects",
            "Projects and their safe cross-domain relationships.",
            Capability.READ,
            listing=ListRead(
                projects.list_projects,
                ProjectQuery,
                ProjectRow,
                sorts=SORTS,
                filters=FILTERS,
                toggles=(NEEDS_OUTPUT, NO_CONTENT, NO_DOCS, UPDATES_ON),
            ),
            detail=DetailRead(projects.get_project, ProjectRecord),
            identifier="slug",
            search=SearchDefinition.of(
                Project,
                "slug",
                ("description", "technologies_used", "notes"),
                identifiers=("slug",),
                title_field="name",
            ),
            web_route="projects:list",
        ),
    )
