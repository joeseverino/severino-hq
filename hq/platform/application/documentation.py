"""Documentation synchronization use case shared by web, MCP, and CLI."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from typing import Annotated, Any, Literal, TypedDict, final

from hq.domains.docs_index.importer import (
    import_manifest_data,
    validate_manifest_data,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.platform.core.audit import audit_operation, record_event
from hq.platform.core.models import AuditLog

from .command_fields import Describe, Names
from .describe import listed
from .entity_links import EntityLink, entity_link
from .labels import plural
from .projection import listing
from .record_errors import PolicyError
from .record_fields import Notes, PublishedAt
from .record_saves import Held, Link, linked_saver
from .references import Mention, Mentions
from .refusal_status import RefusalEnvelope, refuse
from .security import Capability, Principal
from .sensitivity import safe_records
from .ui import counted, ended

MAX_MANIFEST_ITEMS = 2000
SYNC = "documentation.sync"

# A project's own note in the vault is ``project-<slug>``: the document that
# stands for the project, so a page links the project and not the note.
PROJECT_NOTE_PREFIX = "project-"


def document_link(record: DocumentationRecord, *, label: str = "") -> EntityLink:
    """A document by its title, its id on hover."""

    return replace(entity_link("document", record.doc_id, label=label or record.title), title=record.doc_id)


@dataclass(frozen=True, slots=True)
class RelatedDocuments:
    """A record's documents as its page lists them."""

    # One group per type.
    groups: tuple[Mentions, ...] = ()
    # Projects whose own note is among the documents.
    projects: tuple[EntityLink, ...] = ()
    # A writeup's or page's own vault note, apart from what it is related to.
    sources: tuple[EntityLink, ...] = ()


def _noted_projects(records: list[DocumentationRecord]) -> dict[int, Project]:
    """The project each project note stands for, by the note's key."""

    noted = {
        record.doc_id.removeprefix(PROJECT_NOTE_PREFIX): record
        for record in records
        if record.doc_id.startswith(PROJECT_NOTE_PREFIX)
    }
    if not noted:
        return {}
    return {noted[project.slug].pk: project for project in Project.objects.filter(slug__in=noted)}


def related_documents(
    records: Iterable[DocumentationRecord],
    *,
    about: Project | None = None,
    listed: Iterable[Project] = (),
    sources: bool = False,
) -> RelatedDocuments:
    """``records`` grouped by type, in the order ``DocType`` declares.

    A project's own note becomes a link to that project. The note of the
    project the page is ``about`` is left out, and so is a project the page has
    already ``listed``. With ``sources`` an article draft is the page's own
    source document, named by where it is in the vault.
    """

    records = sorted(records, key=lambda record: record.title.lower())
    projects = _noted_projects(records)
    said = {project.slug for project in listed} | ({about.slug} if about else set())
    draft = DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT
    own = [record for record in records if sources and record.doc_type == draft]
    rest = [record for record in records if record.pk not in projects and record not in own]
    groups = [
        Mentions(plural(str(label)), found)
        for value, label in DocumentationRecord.DocType.choices
        if (found := tuple(Mention(document_link(record)) for record in rest if record.doc_type == value))
    ]
    return RelatedDocuments(
        groups=tuple(groups),
        projects=tuple(
            entity_link("project", project.slug, label=project.name)
            for project in sorted(projects.values(), key=lambda project: project.name.lower())
            if project.slug not in said
        ),
        sources=tuple(document_link(record, label=record.obsidian_path) for record in own),
    )


# What a sync of records from a manifest does with the records HQ holds,
# said once for each sync that takes it.
type UpdateExisting = Annotated[bool, Describe("Update records HQ already holds.")]
type PruneOrphans = Annotated[bool, Describe("Delete records the manifest no longer lists.")]
type ConfirmPrune = Annotated[bool, Describe("Confirms the delete that prune_orphans asks for.")]


@dataclass(frozen=True, slots=True)
class DocumentationSyncCommand:
    manifest: Annotated[list[dict[str, Any]], Describe("The documentation records to sync, one per doc.")]
    update_existing: UpdateExisting = True
    report_orphans: Annotated[bool, Describe("Report records the manifest no longer lists.")] = False
    prune_orphans: PruneOrphans = False
    confirm_prune: ConfirmPrune = False


# A doc's fields as its command writes them and its row reads them back, each said once.
type DocId = Annotated[str, Describe("The doc's ID in the vault.")]
type DocumentationTitle = Annotated[str, Describe("The doc's title.")]
type DocType = Annotated[str, Describe(ended(f"What type of doc it is: {listed(DocumentationRecord.DocType.values)}"))]
type DocEnvironment = Annotated[
    str, Describe(ended(f"Where that system runs: {listed(DocumentationRecord.Environment.values)}"))
]
type DocStatus = Annotated[str, Describe(ended(f"Where the doc stands: {listed(DocumentationRecord.Status.values)}"))]
type DocSensitivity = Annotated[
    str,
    Describe(
        f"Who may read it, {listed(DocumentationRecord.Sensitivity.values)}; a restricted doc never reaches an agent."
    ),
]
type ObsidianPath = Annotated[str, Describe("Its path in the vault; empty for none.")]
type GithubPath = Annotated[str, Describe("Its path in a repository; empty for none.")]
type ExternalUrl = Annotated[str, Describe("Where it is published outside HQ; empty for none.")]
type LastReviewed = Annotated[
    date | None, Describe("When it was last checked against what it describes, an ISO 8601 date; null for never.")
]


@dataclass(frozen=True, slots=True)
class DocumentationCommand:
    doc_id: DocId
    title: DocumentationTitle
    doc_type: DocType = "runbook"
    system_service: Annotated[str, Describe("The system or service it is about.")] = ""
    environment: DocEnvironment = "other"
    status: DocStatus = "draft"
    sensitivity: DocSensitivity = "internal"
    obsidian_path: ObsidianPath = ""
    github_path: GithubPath = ""
    external_url: ExternalUrl = ""
    last_reviewed: LastReviewed = None
    published_at: PublishedAt = None
    notes: Notes = ""
    related_projects: Annotated[tuple[str, ...], Names("projects"), Describe("The projects this doc covers.")] = ()
    related_assets: Annotated[tuple[str, ...], Names("assets"), Describe("The assets this doc covers.")] = ()
    related_expenses: Annotated[
        tuple[int, ...], Names("expenses", key="id"), Describe("The expenses this doc records.")
    ] = ()


def serialize_documentation(record: DocumentationRecord, *, relationships: bool = True) -> dict[str, Any]:
    safe = record.is_safe_for_ai_export
    fields: dict[str, Any] = {
        "doc_id": record.doc_id,
        "title": record.title,
        "doc_type": record.doc_type,
        "system": record.system_service,
        "environment": record.environment,
        "status": record.status,
        "sensitivity": record.sensitivity,
        "obsidian_path": record.obsidian_path if safe else "",
        "github_path": record.github_path if safe else "",
        "external_url": record.external_url if safe else "",
        "last_reviewed": (record.last_reviewed.isoformat() if record.last_reviewed else None),
        "published_at": (record.published_at.isoformat() if record.published_at else None),
        "notes": record.notes if safe else "",
        "updated_at": record.updated_at.isoformat(),
    }
    if relationships:
        fields["relationships"] = {
            "projects": list(record.related_projects.order_by("slug").values_list("slug", flat=True)),
            "assets": list(record.related_assets.order_by("slug").values_list("slug", flat=True)),
            "expense_ids": list(record.related_expenses.order_by("id").values_list("id", flat=True)),
        }
    return fields


def list_documentation(*, status: str | None = None, query: str | None = None, limit: int = 50) -> dict[str, Any]:
    """The documentation records any reader may see, by the domain's own
    sensitivity rule: a restricted record is never listed."""

    return listing(
        safe_records(),
        partial(serialize_documentation, relationships=False),
        search=("doc_id", "title", "system_service"),
        status=status,
        query=query,
        order=("doc_id",),
        limit=limit,
    )


save_documentation = linked_saver(
    "documentation",
    serialize_documentation,
    refusals=Held("That documentation record was not found.", "That documentation record changed after it was read."),
    links={
        "related_projects": Link("slug", "A related project was not found."),
        "related_assets": Link("slug", "A related asset was not found."),
        "related_expenses": Link("id", "A related expense was not found."),
    },
)


# What a sync of the vault's docs answers under every name it is published by.
type SyncStats = Annotated[
    dict[str, Any],
    Describe(
        "How the sync went: how many docs it created, updated and skipped, the links naming a record "
        "HQ does not hold, and the orphans found or pruned where asked."
    ),
]


class DocumentationSync(TypedDict):
    """What syncing the vault's docs into HQ did."""

    stats: SyncStats


@final
class DocumentationSynced(DocumentationSync):
    """What syncing the vault's docs into HQ did, as the command answers it."""

    ok: Literal[True]


def sync_documentation(
    manifest: list[dict[str, Any]],
    *,
    principal: Principal,
    update_existing: bool = True,
    report_orphans: bool = False,
    prune_orphans: bool = False,
    confirm_prune: bool = False,
) -> DocumentationSynced | RefusalEnvelope:
    """Validate and atomically synchronize vault metadata into HQ."""

    principal.require(Capability.SYNC_DOCUMENTATION)
    if prune_orphans:
        principal.require(Capability.PRUNE_DOCUMENTATION)
    if len(manifest) > MAX_MANIFEST_ITEMS:
        raise PolicyError(f"Manifest exceeds the {MAX_MANIFEST_ITEMS}-record safety limit.")
    if any(not isinstance(entry, dict) for entry in manifest):
        raise PolicyError("Every manifest record must be a JSON object.")
    problems = validate_manifest_data(manifest)
    if problems:
        return refuse("invalid_input", f"{counted(len(problems), 'record')} refused. Nothing was synced.", problems)
    if prune_orphans and not confirm_prune:
        raise PolicyError("confirm_prune must be true when prune_orphans is true")

    with audit_operation(operation=SYNC, principal=principal):
        stats = import_manifest_data(
            manifest,
            update_existing=update_existing,
            report_orphans=report_orphans or prune_orphans,
            prune_orphans=prune_orphans,
        )
        record_event(
            action=AuditLog.Action.IMPORTED,
            type_label="DocumentationRecord",
            message="Updated the list of docs.",
            metadata={"stats": stats},
        )
    return {"ok": True, "stats": stats}


def execute_documentation_sync(
    command: DocumentationSyncCommand,
    *,
    principal: Principal,
) -> DocumentationSynced | RefusalEnvelope:
    return sync_documentation(
        command.manifest,
        principal=principal,
        update_existing=command.update_existing,
        report_orphans=command.report_orphans,
        prune_orphans=command.prune_orphans,
        confirm_prune=command.confirm_prune,
    )
