"""The projects resource, declared on its domain in application/domains.py."""

from typing import NotRequired, TypedDict

from pydantic import Field, create_model

from hq.platform.application import projects
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import STATUS_FILTER, BoundedQuery, TextFilter, one_of
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import Project

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
    outcome: str
    started_at: str | None
    finished_at: str | None


class Updates(TypedDict):
    """What HQ's update run has open in the repository; a count it could not read is null."""

    open_pulls: int | None
    oldest_pull_days: int | None
    unmatched_alerts: int | None
    last_run: UpdateRun | None


class ProjectRow(TypedDict):
    slug: str
    name: str
    category: str
    status: str
    description: str
    technologies: list[str]
    repository_url: str
    public_url: str
    last_push_at: str | None
    dependency_updates: bool
    updated_at: str
    # Only for a project whose repository HQ can name.
    updates: NotRequired[Updates]


class ProjectLinks(TypedDict):
    assets: list[str]
    content: list[str]
    documentation: list[str]
    expense_ids: list[int]


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
