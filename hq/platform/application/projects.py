"""Project commands and queries.

Web views, MCP tools, and management commands call this module.  It owns the
transaction, validation, persistence, audit attribution, and canonical result
shape; adapters only parse input and render output.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from hq.domains.content.content_sync import ContentSyncError, sync_content_index
from hq.domains.projects.github import GitHubMetadataError, fetch_last_push
from hq.domains.projects.models import Project
from hq.platform.core.audit import operation_context, record_event
from hq.platform.core.models import AuditLog
lazy from hq.platform.application.routes import reverse

from .domains import records_of
from .github_public import github_repository
from .outbound_work import OutboundWork
from .projection import addressable, iso, listing, projection_scope, read_once
from .record_errors import RecordConflict, RecordNotFound
from .record_links import linked
from .security import Principal
from .standards import MET
from .ui import counted
from .upserts import upsert_by_slug
lazy from . import capabilities
lazy from .action_links import READ_NOW_CAPABILITY
lazy from .dependency_updates import last_run
lazy from .entity_links import EntityLink, entity_link
lazy from .github_estate import Repository, repositories, repository_for
lazy from .github_posture import STANDARD, posture_of
lazy from .plugins import installed_plugins
lazy from .timestamps import moment


def repository_key(repository: str) -> str:
    """How HQ compares repositories: ``owner/name``, lower-cased, as GitHub does.

    Takes a repository's URL or its ``owner/name``. A URL that names no GitHub
    repository has no key.
    """

    if "://" not in repository:
        return repository.strip().lower()
    parts = github_repository(repository)
    return "/".join(parts).lower() if parts else ""


def projects_by_repository() -> dict[str, Project]:
    """Every project that names a GitHub repository, by its ``repository_key``.

    Read once per projection. Look one up with ``repository_key`` of the other
    side's URL or name.
    """

    def load() -> dict[str, Project]:
        found = {}
        for project in Project.objects.exclude(repository_url=""):
            if key := repository_key(project.repository_url):
                found[key] = project
        return found

    return read_once("projects.by_repository", load)


def hq_sections(project: Project) -> tuple[tuple[str, str], ...]:
    """The sections of HQ built from this project's repository: ``(name, url)``.

    An installed extension names the repository it is built from, and a
    project names its repository, so the two meet without either naming the
    other. The link opens the extension's first page in the nav.
    """

    repository = repository_key(project.repository_url)
    if not repository:
        return ()
    found = []
    for manifest in installed_plugins():
        if repository_key(manifest.source_repository) != repository or not manifest.navigation:
            continue
        home = min(manifest.navigation, key=lambda item: item.order)
        found.append((manifest.name, reverse(home.route)))
    return tuple(found)


def built_from(plugin_id: str) -> EntityLink | None:
    """The project an installed extension is built from, as a link to its page.

    The other end of ``hq_sections``: the extension names its repository and a
    project names the same one. None when no project does.
    """

    wanted = next(
        (repository_key(manifest.source_repository) for manifest in installed_plugins() if manifest.id == plugin_id),
        "",
    )
    project = projects_by_repository().get(wanted) if wanted else None
    return entity_link("project", project.slug, label=project.name) if project else None


GitHubFetcher = Callable[..., datetime | None]
ContentSync = Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
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


def serialize_project(project: Project, *, relationships: bool = False) -> dict[str, Any]:
    fields = {
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
    if repository_key(project.repository_url):
        fields["updates"] = _updates(project)
    return linked(fields, project, relationships=relationships, assets=project.assets)


def _updates(project: Project) -> dict[str, Any]:
    """Whether HQ's update run covers the repository, and what it has open there.

    A count the reading does not hold is None: a repository the App does not
    read, or whose Dependabot alerts GitHub refused, is not one with none.
    """

    repository = repository_for(project.repository_url)
    read = repository is not None
    run = last_run(project.repository_url)
    return {
        "enabled": project.dependency_updates,
        "open_pulls": len(repository.update_pulls) if read else None,
        "oldest_pull_days": repository.oldest_update_days if read else None,
        "unmatched_alerts": (
            len(repository.unmatched_alerts) if read and repository.dependabot_alerts is not None else None
        ),
        "last_run": run.as_record() if run is not None else None,
    }


def list_projects(
    *,
    status: str | None = None,
    query: str | None = None,
    updates: bool | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    rows = Project.objects.all() if updates is None else Project.objects.filter(dependency_updates=updates)
    # Every item reads the same stored GitHub reading.
    with projection_scope():
        return listing(
            rows,
            serialize_project,
            search=("name", "slug", "description", "technologies_used"),
            status=status,
            query=query,
            limit=limit,
        )


def get_project(slug: str) -> dict[str, Any]:
    return addressable(Project, serialize_project, slug)


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

    principal.require(records_of("projects").write)
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist as exc:
        raise RecordNotFound("That project was not found.") from exc

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
                    message="Updated the site's content.",
                    metadata=stats,
                )
            result["content"] = {"ok": True, **stats}
        except ContentSyncError as exc:
            result["content"] = {"ok": False, "error": str(exc)}

    if not project.repository_url:
        result["github"] = {"ok": False, "error": "This project has no GitHub repository address."}
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
        result["github"] = {"ok": False, "error": "GitHub did not say when this repository was last pushed."}
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

    repository = repository_for(repository_url)
    connection_ref = str(repository.record.get("connection_ref") or "") if repository else ""
    if not connection_ref:
        return None
    answer = capabilities.execute_capability(
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

    return moment(stamp)


def _record_push(project: Project, pushed_at: datetime | None, principal: Principal) -> dict[str, Any]:
    """Persist when the repository last moved, where a source said."""

    if pushed_at is None:
        return {"ok": True, "last_push_at": iso(project.last_push_at) or None}
    with (
        transaction.atomic(),
        operation_context(
            interface=principal.interface,
            actor=principal.actor,
            operation="project.refresh",
        ),
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


REFRESH = "project.refresh"


def refresh_summary(result: dict[str, Any]) -> str:
    """What a refresh did, in a sentence a person reads when it ends."""

    said = []
    content = result.get("content")
    if content and content["ok"]:
        said.append(
            f"Read {counted(content['total'], 'writeup or page', 'writeups and pages')} from the site "
            f"({content['created']} new, {content['updated']} changed)."
        )
    elif content:
        said.append(f"The site's list of writeups and pages could not be read: {content['error']}")
    app, github = result.get("github_app"), result.get("github")
    if app and app["ok"]:
        said.append(app["message"])
    else:
        if app:
            said.append(f"GitHub was not read: {app['error']}")
        if github and github["ok"]:
            said.append("Read the last push from GitHub.")
        elif github:
            said.append(github["error"])
    return " ".join(said)


def _refresh(progress: Any, *, subject: str, principal: Principal) -> dict[str, Any]:
    progress("Reading GitHub.")
    result = refresh_project(subject, principal=principal)
    progress(refresh_summary(result), force=True)
    return result


def _known(slug: str) -> str:
    """A refresh is asked for a project HQ holds; any other is not found, as
    any command's unknown target is, before a job is recorded."""

    if not Project.objects.filter(slug=slug).exists():
        raise RecordNotFound("That project was not found.")
    return ""


def outbound_work() -> tuple[OutboundWork, ...]:
    """A refresh waits on GitHub and on the published site, so it is done off
    the request, one at a time, and says what it did as its last note."""

    return (
        OutboundWork(
            REFRESH,
            "Refresh a project",
            "Read a project's latest GitHub activity and published pages.",
            records_of("projects").write,
            _refresh,
            subject_label="Project",
            subject_help="The project to refresh.",
            subject_resource="projects",
            refuse=_known,
            verb="Refresh",
            execution_notes=(
                "Where the GitHub App reads the repository, ask the controller to read that "
                "connection now, through infrastructure.controller.refresh.",
                "For a repository the App does not read, read its last push from GitHub's "
                "public API, which needs no credential.",
                "Save when it was last pushed to, and record who asked.",
            ),
        ),
    )


@transaction.atomic
def save_project(
    command: ProjectCommand,
    *,
    principal: Principal,
    current_slug: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Create or update one project and return the canonical representation."""

    principal.require(records_of("projects").write)
    operation = "project.create" if current_slug is None else "project.update"
    with operation_context(interface=principal.interface, actor=principal.actor, operation=operation):
        created = current_slug is None
        project = Project() if current_slug is None else _locked(current_slug, expected_updated_at)
        for field, value in asdict(command).items():
            setattr(project, field, value)
        project.full_clean()
        project.save()

    return {
        "ok": True,
        "created": created,
        "project": serialize_project(project, relationships=True),
    }


def _locked(slug: str, expected_updated_at: str | None) -> Project:
    """The project to change, held for this transaction, as the caller last read it."""

    try:
        project = Project.objects.select_for_update().get(slug=slug)
    except Project.DoesNotExist as exc:
        raise RecordNotFound("That project was not found.") from exc
    if expected_updated_at and project.updated_at.isoformat() != expected_updated_at:
        raise RecordConflict("That project changed after it was read.")
    return project


DEPENDENCY_UPDATES = "project.dependency_updates"
# The repository check updates rest on: Renovate opens its security fixes from
# these alerts.
UPDATES_NEED = "vulnerability-alerts"


@dataclass(frozen=True, slots=True)
class DependencyUpdatesCommand:
    """Whether HQ's dependency update run covers the project's repository."""

    enabled: bool


@transaction.atomic
def set_dependency_updates(
    command: DependencyUpdatesCommand,
    *,
    principal: Principal,
    current_slug: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Opt a project's repository in to dependency updates, or out.

    Only a project that names a repository, and turning updates on only while
    the repository passes ``UPDATES_NEED``. The switch records intent; the run
    that acts on it reads it later.
    """

    principal.require(records_of("projects").write)
    project = _locked(current_slug, expected_updated_at)
    refused = _updates_refusal(project, enabling=command.enabled)
    if refused:
        return {"ok": False, "error": {"code": "precondition", "message": refused}}
    if project.dependency_updates != command.enabled:
        project.dependency_updates = command.enabled
        project.full_clean()
        project.save()
    return {"ok": True, "project": serialize_project(project, relationships=True)}


def _updates_refusal(project: Project, *, enabling: bool) -> str:
    """Why the switch cannot be set as asked; "" when it can."""

    if not project.repository_url:
        return "This project names no repository, so there is nothing to update."
    if not enabling:
        return ""
    repository = repository_for(project.repository_url)
    posture = posture_of(repository) if repository is not None else None
    if posture is not None and posture.state_of(UPDATES_NEED) == MET:
        return ""
    check = next(check for check in STANDARD if check.id == UPDATES_NEED)
    return f'Dependency updates need the repository check "{check.label}" ({check.id}) to pass first. {check.fix}'


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


def sync_repositories(*, principal: Principal) -> dict[str, Any]:
    """Make every repository the GitHub App reads a project.

    A repository joins the project whose URL names it; else a project with no
    repository whose slug is the repository's name, which takes its URL; else
    a new project, archived when the repository is. Nothing else on a project
    is changed and nothing is deleted, so a second pass over the same reading
    writes nothing.
    """

    known = set(projects_by_repository())
    unattached = {project.slug.lower(): project for project in Project.objects.filter(repository_url="")}
    attached: list[str] = []
    created: list[str] = []
    for name, repository in sorted(repositories().items()):
        key = repository_key(name)
        if not key or key in known:
            continue
        named = unattached.pop(repository.short.lower(), None)
        try:
            result = _take_on(repository, named, principal=principal)
        except ValidationError, RecordConflict, RecordNotFound:
            # One repository that cannot be a project must not stop the rest;
            # the next sweep tries it again.
            continue
        known.add(key)
        (attached if named else created).append(result["project"]["slug"])
    return {"attached": attached, "created": created}


def _take_on(repository: Repository, named: Project | None, *, principal: Principal) -> dict[str, Any]:
    """Attach ``repository`` to ``named``, or make it a project of its own."""

    url = f"https://github.com/{repository.name}"
    if named is not None:
        kept = {field.name: getattr(named, field.name) for field in fields(ProjectCommand)}
        command = replace(ProjectCommand(**kept), repository_url=url)
        return save_project(command, principal=principal, current_slug=named.slug)
    status = Project.Status.ARCHIVED if repository.archived else Project.Status.ACTIVE
    return save_project(ProjectCommand(name=repository.short, status=status, repository_url=url), principal=principal)
