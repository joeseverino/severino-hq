"""The projects resource, declared on its domain in application/domains.py."""

from typing import Annotated, NotRequired, TypedDict

from pydantic import Field, create_model

from hq.platform.application import projects
from hq.platform.application.answer_fields import LinkedContent, LinkedDocs, LinkedExpenses, RecordSlug, UpdatedAt
from hq.platform.application.describe import Describe, listed
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability
from hq.platform.application.ui import ended

from .models import PROJECT_CATEGORY_CHOICES, Project

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
    name: projects.ProjectName
    category: Annotated[
        str, Describe(ended(f"What type of project it is: {listed(v for v, _ in PROJECT_CATEGORY_CHOICES)}"))
    ]
    status: Annotated[str, Describe(ended(f"Where it stands: {listed(Project.Status.values)}"))]
    description: Annotated[str, Describe("What it is, in a few sentences; empty when none was written.")]
    technologies: Annotated[list[str], Describe("The technologies it uses, in the order they were written.")]
    repository_url: Annotated[str, Describe("The address of its code repository; empty for none.")]
    public_url: Annotated[str, Describe("Where it can be seen published; empty for none.")]
    last_push_at: Annotated[
        str | None,
        Describe("When its repository was last pushed to, an ISO 8601 date-time; null until HQ has read it."),
    ]
    dependency_updates: Annotated[bool, Describe("Whether HQ's dependency update run covers its repository.")]
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
            projects.list_projects,
            ProjectQuery,
            projects.get_project,
            "slug",
            search=SearchDefinition(
                "projects",
                Project,
                "slug",
                ("name", "slug", "description", "technologies_used", "notes"),
                label="Projects",
                title_field="name",
            ),
            web_route="projects:list",
            row_type=ProjectRow,
            record_type=ProjectRecord,
        ),
    )
