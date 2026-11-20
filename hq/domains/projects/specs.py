"""The projects' commands and resource, declared on its domain in application/domains.py."""

from typing import Annotated, NotRequired, TypedDict

from pydantic import Field, create_model

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
from hq.platform.application.integration_specs import CapabilitySpec, ListRead, ResourceSpec
from hq.platform.application.record_fields import Notes, PublishedUrl
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import Project


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "hq.import",
            "Import projects and assets from one file. Nothing is saved unless every record in it is valid.",
            Effect.REMOTE_WRITE,
            # The capabilities of the upserts it runs, both of them.
            registry_import.REQUIRED_CAPABILITIES,
            registry_import.HQImportCommand,
            registry_import.execute_hq_import,
            subject_resource="projects",
            execution_notes=(
                "Validate every record before writing any.",
                "Upsert each record through project.upsert or asset.upsert, in one transaction.",
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


ProjectQuery = create_model(
    "ProjectQuery",
    __base__=BoundedQuery,
    status=(one_of(Project.Status.values) | None, Field(default=None, description=STATUS_FILTER)),
    query=(TextFilter, None),
    updates=(
        bool | None,
        Field(
            default=None,
            description="True for the projects HQ's dependency update run covers, false for the rest; absent, both.",
        ),
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
            listing=ListRead(projects.list_projects, ProjectQuery, ProjectRow),
            detail_handler=projects.get_project,
            identifier="slug",
            search=SearchDefinition(
                "projects",
                Project,
                "slug",
                ("name", "slug", "description", "technologies_used", "notes"),
                label="Projects",
                title_field="name",
            ),
            web_route="projects:list",
            record_type=ProjectRecord,
        ),
    )
