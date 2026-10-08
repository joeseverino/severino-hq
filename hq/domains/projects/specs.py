"""The projects resource, declared on its domain in application/domains.py."""

from hq.platform.application import projects
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.search_contracts import SearchDefinition
from hq.platform.application.security import Capability

from .models import Project


class ProjectQuery(BoundedQuery):
    status: str | None = None
    query: str | None = None


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
            not_found_errors=(projects.NotFoundError,),
            search=SearchDefinition(
                "projects",
                Project,
                "slug",
                ("name", "slug", "description", "technologies_used", "notes"),
                label="Projects",
                title_field="name",
            ),
            web_route="projects:list",
        ),
    )
