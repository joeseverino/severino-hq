"""Project commands and queries.

Web views, MCP tools, and management commands call this module.  It owns the
transaction, validation, persistence, audit attribution, and canonical result
shape; adapters only parse input and render output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable

from django.conf import settings
from django.db import transaction

from core.audit import operation_context, record_event
from core.models import AuditLog
from docs_index.models import DocumentationRecord
from projects.models import PROJECT_CATEGORY_CHOICES, Project
from projects.github import GitHubMetadataError, fetch_last_push
from content.content_sync import ContentSyncError, sync_content_index
from .security import Capability, Principal
from .projection import addressable, iso, listing

SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)


class NotFoundError(ValueError):
    """A requested HQ object does not exist."""


class ConflictError(ValueError):
    """The caller tried to write over a newer version of an object."""


GitHubFetcher = Callable[..., datetime | None]
ContentSync = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class ProjectCommand:
    name: str
    slug: str = ""
    category: str = "other"
    status: str = Project.Status.IDEA
    description: str = ""
    technologies_used: str = ""
    repository_url: str = ""
    public_url: str = ""
    deployment_notes: str = ""
    security_notes: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ProjectRefreshCommand:
    """A targeted refresh has no free-form payload beyond its project target."""

    pass






def serialize_project(project: Project, *, relationships: bool = False) -> dict[str, Any]:
    result = {
        "slug": project.slug,
        "name": project.name,
        "category": project.category,
        "status": project.status,
        "description": project.description,
        "technologies": project.tech_list,
        "repository_url": project.repository_url,
        "public_url": project.public_url,
        "last_push_at": iso(project.last_push_at),
        "updated_at": iso(project.updated_at),
    }
    if relationships:
        result["relationships"] = {
            "documentation": list(
                project.documentation_records.filter(
                    sensitivity__in=SAFE_SENSITIVITIES
                )
                .order_by("doc_id")
                .values_list("doc_id", flat=True)
            ),
            "content": list(
                project.content_items.order_by("slug").values_list("slug", flat=True)
            ),
            "assets": list(
                project.assets.order_by("slug").values_list("slug", flat=True)
            ),
            "expense_ids": list(
                project.expenses.order_by("-date", "-id").values_list("id", flat=True)
            ),
        }
    return result


def list_projects(
    *, status: str | None = None, query: str | None = None, limit: int = 50
) -> dict[str, Any]:
    return listing(
        Project,
        serialize_project,
        search=("name", "slug", "description", "technologies_used"),
        status=status,
        query=query,
        limit=limit,
    )


def get_project(slug: str) -> dict[str, Any]:
    return addressable(
        Project, serialize_project, slug, label="Project", missing=NotFoundError
    )


def refresh_project(
    slug: str,
    *,
    principal: Principal,
    github_fetcher: GitHubFetcher = fetch_last_push,
    content_sync: ContentSync = sync_content_index,
) -> dict[str, Any]:
    """Refresh external project metadata through injected integration gateways."""

    principal.require(Capability.WRITE_PROJECTS)
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist as exc:
        raise NotFoundError(f"Project {slug!r} was not found.") from exc

    result: dict[str, Any] = {"ok": True, "content": None, "github": None}
    if slug == getattr(settings, "CONTENT_INDEX_PROJECT_SLUG", ""):
        try:
            with operation_context(
                interface=principal.interface,
                actor=principal.actor,
                operation="project.refresh_content",
            ):
                stats = content_sync()
                record_event(
                    action=AuditLog.Action.UPDATED,
                    obj=project,
                    type_label="Project",
                    message="Synchronized the published content index.",
                    metadata=stats,
                )
            result["content"] = {"ok": True, **stats}
        except ContentSyncError as exc:
            result["content"] = {"ok": False, "error": str(exc)}

    if not project.repository_url:
        result["github"] = {"ok": False, "error": "Project has no GitHub repository URL."}
        return result

    try:
        pushed_at = github_fetcher(
            project.repository_url,
            token=getattr(settings, "GITHUB_API_TOKEN", ""),
        )
    except GitHubMetadataError as exc:
        result["github"] = {"ok": False, "error": str(exc)}
        return result

    if pushed_at is None:
        result["github"] = {"ok": False, "error": "GitHub returned no push metadata."}
        return result

    with transaction.atomic(), operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="project.refresh",
    ):
        project = Project.objects.select_for_update().get(pk=project.pk)
        project.last_push_at = pushed_at
        project.save(update_fields=["last_push_at", "updated_at"])
    result["github"] = {"ok": True, "last_push_at": pushed_at.isoformat()}
    return result


def execute_project_refresh(
    command: ProjectRefreshCommand,
    *,
    principal: Principal,
    current_slug: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Capability-shaped entry point for the existing project refresh use case."""

    del command, expected_updated_at
    return refresh_project(current_slug, principal=principal)


@transaction.atomic
def save_project(
    command: ProjectCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Create or update one project and return the canonical representation."""

    principal.require(Capability.WRITE_PROJECTS)
    operation = "project.create" if current_slug is None else "project.update"
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_slug is None:
            project = Project()
            created = True
        else:
            try:
                project = Project.objects.select_for_update().get(slug=current_slug)
            except Project.DoesNotExist as exc:
                raise NotFoundError(
                    f"Project {current_slug!r} was not found."
                ) from exc
            created = False
            if (
                expected_updated_at
                and project.updated_at.isoformat() != expected_updated_at
            ):
                raise ConflictError(
                    f"Project {current_slug!r} changed after it was read."
                )

        for field, value in asdict(command).items():
            setattr(project, field, value)
        project.full_clean()
        project.save()

    return {
        "ok": True,
        "created": created,
        "project": serialize_project(project, relationships=True),
    }


@transaction.atomic
def upsert_project(
    command: ProjectCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Idempotently create or update a project by its command slug."""

    current_slug = (
        command.slug
        if Project.objects.select_for_update().filter(slug=command.slug).exists()
        else None
    )
    return save_project(
        command,
        principal=principal,
        current_slug=current_slug,
        expected_updated_at=expected_updated_at,
    )


def project_command_from_cleaned_data(data: dict[str, Any]) -> ProjectCommand:
    """Translate the shared ModelForm's validated fields into the use-case DTO."""

    return ProjectCommand(
        **{
            field: data.get(field, "")
            for field in ProjectCommand.__dataclass_fields__
        }
    )


def project_choices() -> dict[str, list[str]]:
    return {
        "categories": [value for value, _ in PROJECT_CATEGORY_CHOICES],
        "statuses": [choice.value for choice in Project.Status],
    }
