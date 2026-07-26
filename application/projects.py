"""Project commands and queries.

Web views, MCP tools, and management commands call this module.  It owns the
transaction, validation, persistence, audit attribution, and canonical result
shape; adapters only parse input and render output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from django.db import transaction
from django.db.models import Q

from core.audit import operation_context
from docs_index.models import DocumentationRecord
from projects.models import PROJECT_CATEGORY_CHOICES, Project
from .security import Capability, Principal

MAX_PAGE_SIZE = 100
SAFE_SENSITIVITIES = (
    DocumentationRecord.Sensitivity.PUBLIC,
    DocumentationRecord.Sensitivity.INTERNAL,
)


class NotFoundError(ValueError):
    """A requested HQ object does not exist."""


class ConflictError(ValueError):
    """The caller tried to write over a newer version of an object."""


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


def _page_size(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


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
        "last_push_at": _iso(project.last_push_at),
        "updated_at": _iso(project.updated_at),
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
    qs = Project.objects.all()
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(
            Q(name__icontains=query)
            | Q(slug__icontains=query)
            | Q(description__icontains=query)
            | Q(technologies_used__icontains=query)
        )
    items = [
        serialize_project(project)
        for project in qs.order_by("slug")[: _page_size(limit)]
    ]
    return {"items": items, "count": len(items)}


def get_project(slug: str) -> dict[str, Any]:
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist as exc:
        raise NotFoundError(f"Project {slug!r} was not found.") from exc
    return serialize_project(project, relationships=True)


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
