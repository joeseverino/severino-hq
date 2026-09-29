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
from projects.models import Project
from projects.github import GitHubMetadataError, fetch_last_push
from content.content_sync import ContentSyncError, sync_content_index
from .sensitivity import safe_doc_ids
from .security import Capability, Principal
from .upserts import upsert_by_slug
from .projection import addressable, iso, listing


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
            "documentation": safe_doc_ids(project.documentation_records),
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
    """Refresh external project metadata through injected integration gateways.

    A repository the GitHub App reads is refreshed by asking the controller to
    read that connection now; one it does not read falls back to the anonymous
    public read of when it was last pushed.
    """

    principal.require(Capability.WRITE_PROJECTS)
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist as exc:
        raise NotFoundError(f"Project {slug!r} was not found.") from exc

    result: dict[str, Any] = {"ok": True, "content": None, "github": None, "github_app": None}
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
                    message="Synced the content index.",
                    metadata=stats,
                )
            result["content"] = {"ok": True, **stats}
        except ContentSyncError as exc:
            result["content"] = {"ok": False, "error": str(exc)}

    if not project.repository_url:
        result["github"] = {"ok": False, "error": "Project has no GitHub repository URL."}
        return result

    app = request_app_read(project.repository_url, principal=principal)
    if app is not None:
        result["github_app"] = app
        if app["ok"]:
            result["github"] = _record_push(project, app.pop("pushed_at"), principal)
            return result

    try:
        pushed_at = github_fetcher(project.repository_url)
    except GitHubMetadataError as exc:
        result["github"] = {"ok": False, "error": str(exc)}
        return result

    if pushed_at is None:
        result["github"] = {"ok": False, "error": "GitHub returned no push metadata."}
        return result
    result["github"] = _record_push(project, pushed_at, principal)
    return result


def request_app_read(repository_url: str, *, principal: Principal) -> dict[str, Any] | None:
    """Ask the controller to read the GitHub App connection that reads this repository.

    None when the App does not read it, which leaves the anonymous public read
    as the only source. Otherwise the same request the Connections page's Read
    now makes, through the same capability: whether this principal may wake the
    controller, and whether policy holds the request for a person, are decided
    there and nowhere else. The pull requests, checks and workflows on the
    project page follow on the controller's next pass.
    """

    from .action_links import READ_NOW_CAPABILITY
    from .capabilities import execute_capability
    from .github_estate import repository_for

    repository = repository_for(repository_url)
    connection_ref = str(repository.record.get("connection_ref") or "") if repository else ""
    if not connection_ref:
        return None
    answer = execute_capability(
        READ_NOW_CAPABILITY, {"connection_ref": connection_ref}, principal=principal
    )
    if answer.get("ok") and answer.get("requested"):
        return {
            "ok": True,
            "connection_ref": connection_ref,
            "message": str(answer.get("message") or ""),
            "pushed_at": _pushed_at(repository.record.get("pushed_at")),
        }
    error = answer.get("error") or {}
    return {
        "ok": False,
        "connection_ref": connection_ref,
        "error": str(error.get("message") or answer.get("message") or "The controller was not asked."),
    }


def _pushed_at(stamp: Any) -> datetime | None:
    """The App's last read of when the repository moved, if it read one."""

    from .timestamps import moment

    return moment(stamp)


def _record_push(
    project: Project, pushed_at: datetime | None, principal: Principal
) -> dict[str, Any]:
    """Persist when the repository last moved, where a source said."""

    if pushed_at is None:
        return {"ok": True, "last_push_at": iso(project.last_push_at) or None}
    with transaction.atomic(), operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="project.refresh",
    ):
        project = Project.objects.select_for_update().get(pk=project.pk)
        project.last_push_at = pushed_at
        # `updated_at` is left out on purpose: it is the "Updated" column, the
        # default sort and this model's ordering, and reading GitHub is not an
        # operator edit. When the repository last moved is `last_push_at`. It
        # is also the `expected_updated_at` token, which a refresh must not
        # invalidate mid-edit.
        project.save(update_fields=["last_push_at"])
    return {"ok": True, "last_push_at": pushed_at.isoformat()}


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


def upsert_project(
    command: ProjectCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Idempotently create or update a project by its command slug."""

    return upsert_by_slug(
        Project,
        command,
        save_project,
        principal=principal,
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
