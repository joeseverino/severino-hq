"""Documentation synchronization use case shared by web, MCP, and CLI."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import partial
from typing import Annotated, Any, Literal, NotRequired, Required, TypeAliasType, TypedDict, final

from pydantic import ConfigDict, Field

from hq.domains.docs_index import frontmatter_schema
from hq.domains.docs_index.importer import (
    CONTENT_DOC_TYPE,
    CONTENT_FILLED,
    CONTENT_PRESENT,
    import_manifest_data,
    validate_manifest_data,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog

from .answer_fields import CallMessage, LinkedAssets, LinkedProjects, PageUrl, RecordedExpenses, UpdatedAt
from .command_fields import Day, Describe, Names, NonEmpty
from .entity_links import EntityLink, entity_link
from .labels import plural
from .projection import listing
from .record_errors import PolicyError, RecordNotFound
from .record_fields import Notes, PublishedAt, RecordId, one_of
from .record_saves import Held, Link, linked_saver, row
from .references import Kind, Mention, Mentions
from .refusal_codes import Code
from .refusal_status import RefusalEnvelope, refuse
from .security import Capability, Principal
from .sensitivity import readable_records, reads_whole
from .ui import counted

MAX_MANIFEST_ITEMS = 2000
SYNC = "documentation.sync"

# A project's own note in the vault is ``project-<slug>``: the document that
# stands for the project, so a page links the project and not the note.
PROJECT_NOTE_PREFIX = "project-"


def document_link(doc_id: str, title: str) -> EntityLink:
    """A document by its title, its id on hover."""

    return replace(entity_link("document", doc_id, label=title), title=doc_id)


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
        if (
            found := tuple(
                Mention(document_link(record.doc_id, record.title)) for record in rest if record.doc_type == value
            )
        )
    ]
    return RelatedDocuments(
        groups=tuple(groups),
        projects=tuple(
            entity_link("project", project.slug, label=project.name)
            for project in sorted(projects.values(), key=lambda project: project.name.lower())
            if project.slug not in said
        ),
        sources=tuple(document_link(record.doc_id, record.obsidian_path) for record in own),
    )


# What a sync of records from a manifest does with the records HQ holds,
# said once for each sync that takes it.
type UpdateExisting = Annotated[bool, Describe("Update records HQ already holds.")]
type PruneOrphans = Annotated[bool, Describe("Delete records the manifest no longer lists.")]
type ConfirmPrune = Annotated[bool, Describe("Confirms the delete that prune_orphans asks for.")]


@dataclass(frozen=True, slots=True)
class DocumentationSyncCommand:
    manifest: Annotated[
        list[ManifestEntry], Field(title="Docs"), Describe("The documentation records to sync, one per doc.")
    ]
    update_existing: UpdateExisting = True
    report_orphans: Annotated[
        bool, Field(title="Report unlisted docs"), Describe("Report records the manifest no longer lists.")
    ] = False
    prune_orphans: Annotated[PruneOrphans, Field(title="Delete unlisted docs")] = False
    confirm_prune: Annotated[ConfirmPrune, Field(title="Confirm the delete")] = False


# A doc's fields as its command writes them and its row reads them back, each said once.
type DocId = Annotated[str, Describe("The doc's ID in the vault.")]
type DocumentationTitle = Annotated[str, Describe("The doc's title.")]
_DOC_TYPE = one_of(DocumentationRecord.DocType.values)
_ENVIRONMENT = one_of(DocumentationRecord.Environment.values)
_STATUS = one_of(DocumentationRecord.Status.values)
_SENSITIVITY = one_of(DocumentationRecord.Sensitivity.values)
type DocType = Annotated[_DOC_TYPE, Describe("What type of doc it is.")]
type DocEnvironment = Annotated[_ENVIRONMENT, Describe("Where that system runs.")]
type DocStatus = Annotated[_STATUS, Describe("Where the doc stands.")]
type DocSensitivity = Annotated[_SENSITIVITY, Describe("Who may read it; a restricted doc never reaches an agent.")]
type ObsidianPath = Annotated[str, Describe("Its path in the vault.", api="Empty for none.")]
type GithubPath = Annotated[str, Describe("Its path in a repository.", api="Empty for none.")]
type ExternalUrl = Annotated[str, Describe("Where it is published outside HQ.", api="Empty for none.")]
type LastReviewed = Annotated[Day | None, Describe("When the doc last changed in the vault.", api="Null for unknown.")]
type DocNotes = Annotated[str, Describe("Index notes only. No runbook text or secrets.")]


# A manifest entry's fields, each as the vault's manifest copies it from a
# doc's frontmatter: a field the importer requires is sent filled, any other
# may be absent or null, and a key HQ does not read is passed over. A field
# its record's command already types is that type, and a doc's id and type
# are each shape's own.
_DOC_ID_SAID = Describe("The doc's ID in the vault.")
_DOC_TYPE_SAID = Describe("What type of doc it is.")
_ENTRY_FIELDS: dict[str, tuple[Any, Describe] | TypeAliasType] = {
    "doc_id": (str, _DOC_ID_SAID),
    "title": (str, Describe("The doc's title.")),
    "doc_type": (str, _DOC_TYPE_SAID),
    "system": (str, Describe("The system or service the doc is about.")),
    "environment": DocEnvironment,
    "status": DocStatus,
    "sensitivity": DocSensitivity,
    "path": (str, Describe("Where the doc is in the vault.")),
    "github_path": (str, Describe("Its path in a repository.")),
    "external_url": (str, Describe("Where it is published outside HQ.")),
    "last_reviewed": (Day, Describe("When the doc last changed in the vault.")),
    "published_at": (Day, Describe("When it was published.")),
    "notes": DocNotes,
    "related_projects": (list[str], Describe("The slugs of the projects the doc is about.")),
    "related_assets": (list[str], Describe("The slugs of the assets the doc is about.")),
    "content_type": (str, Describe("For site content, what it is on the site, such as `portfolio_article` or `page`.")),
    "slug": (str, Describe("For site content, the slug of the item it mirrors to.")),
    "published": (bool, Describe("For site content, whether it is live on the site.")),
    "topic": (str, Describe("For site content, what it is about, in a line.")),
    "tags": (list[str], Describe("The doc's tags.")),
}
# A vault doc's id begins with one of the vault's prefixes; a content entry's
# belongs to the site, so it need only hold something. A prefix's hyphen is
# left bare, since a schema's pattern refuses one escaped outside a class.
_PREFIXES = "|".join(re.sub(r"[^\w-]", r"\\\g<0>", prefix) for prefix in frontmatter_schema.DOC_ID_PREFIXES)
_VAULT_DOC_ID = rf"^\s*(?:{_PREFIXES})"


# Each field's type, by the component name the document writes it under once.
_ENTRY_TYPES: dict[str, TypeAliasType] = {}


def _entry_type(field: str, value: Any, word: str) -> TypeAliasType:
    """A manifest field's named type, which each shape taking it alike refers
    to; one a shape narrows is named with that shape's ``word``."""

    camel = "".join(part.title() for part in field.split("_"))
    for name in (f"Manifest{camel}", f"Manifest{word}{camel}"):
        named = _ENTRY_TYPES.setdefault(name, TypeAliasType(name, value))
        if named.__value__ == value:
            return named
    raise ValueError(f"the manifest field {field} has two {word} types")


def _entry_shape(name: str, doc: str, *, word: str, doc_type: Any, doc_id: Any, content: Any) -> Any:
    """One shape of a manifest entry the importer takes: the frontmatter
    schema's required fields filled, each choice one of its set, and what
    ``content`` says of the entry's ``content_type``: ``True`` for site
    content, which carries what it mirrors to, ``""`` for a doc that is not."""

    filled = {*frontmatter_schema.REQUIRED_FIELDS, *(("content_type", *CONTENT_FILLED) if content is True else ())}
    present = set(CONTENT_PRESENT) if content is True else set()
    shape: dict[str, Any] = {}
    for field, declared in _ENTRY_FIELDS.items():
        typed = {"doc_type": doc_type, "doc_id": doc_id}.get(field)
        if typed is None and isinstance(declared, TypeAliasType):
            typed = declared
        elif typed is None:
            kind, described = declared
            rules: tuple[Any, ...] = (NonEmpty(),) if kind in (str, list[str]) and field in filled else ()
            if field == "content_type" and content == "":
                kind = Literal[""]
            typed = _entry_type(field, Annotated[kind, *rules, described], word)
        if field in filled:
            shape[field] = Required[typed]
        elif field in present:
            shape[field] = Required[typed | None]
        else:
            shape[field] = NotRequired[typed | None]
    entry: Any = TypedDict(name, shape)  # type: ignore[misc]  # built from the frontmatter schema
    entry.__doc__ = doc
    entry.__pydantic_config__ = ConfigDict(extra="allow")
    return entry


type ManifestDocType = Annotated[one_of(sorted(frontmatter_schema.DOC_TYPES - {CONTENT_DOC_TYPE})), _DOC_TYPE_SAID]
type ManifestArticleDocType = Annotated[one_of([CONTENT_DOC_TYPE.value]), _DOC_TYPE_SAID]
type ManifestVaultDocId = Annotated[str, Field(pattern=_VAULT_DOC_ID), _DOC_ID_SAID]
type ManifestSiteDocId = Annotated[str, Field(pattern=r"\S"), _DOC_ID_SAID]
type ManifestEntry = Annotated[
    (
        _entry_shape(
            "ManifestDoc",
            "A doc of the vault's manifest: its frontmatter, its path, and what it relates to.",
            word="",
            doc_type=ManifestDocType,
            doc_id=ManifestVaultDocId,
            content=None,
        )
        | _entry_shape(
            "ManifestDraft",
            "A public article's draft in the vault, which no site item mirrors.",
            word="Draft",
            doc_type=ManifestArticleDocType,
            doc_id=ManifestVaultDocId,
            content="",
        )
        | _entry_shape(
            "ManifestContent",
            "Site content: a public article the site item of its slug mirrors.",
            word="Site",
            doc_type=ManifestArticleDocType,
            doc_id=ManifestSiteDocId,
            content=True,
        )
    ),
    Describe("A doc of the manifest, in the shape its doc type and content take."),
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
    github_path: Annotated[GithubPath, Field(title="GitHub path")] = ""
    external_url: ExternalUrl = ""
    last_reviewed: Annotated[LastReviewed, Field(title="Last changed")] = None
    published_at: PublishedAt = None
    notes: DocNotes = ""
    related_projects: Annotated[tuple[str, ...], Names("projects"), Describe("The projects this doc covers.")] = ()
    related_assets: Annotated[tuple[str, ...], Names("assets"), Describe("The assets this doc covers.")] = ()
    related_expenses: Annotated[
        tuple[RecordId, ...], Names("expenses", key="id"), Describe("The expenses this doc records.")
    ] = ()


class DocumentationRow(TypedDict):
    """One documentation pointer; a path or note it may not show is blank."""

    doc_id: DocId
    title: DocumentationTitle
    doc_type: DocType
    system: Annotated[str, Describe("The system or service it is about; empty for none.")]
    environment: DocEnvironment
    status: DocStatus
    sensitivity: DocSensitivity
    obsidian_path: ObsidianPath
    github_path: GithubPath
    external_url: ExternalUrl
    last_reviewed: LastReviewed
    published_at: PublishedAt
    notes: Notes
    updated_at: UpdatedAt


class DocumentationLinks(TypedDict):
    """The records a documentation pointer is linked to."""

    projects: LinkedProjects
    assets: LinkedAssets
    expense_ids: RecordedExpenses


class DocumentationItem(DocumentationRow):
    """One documentation pointer with the records it is linked to."""

    url: PageUrl
    relationships: DocumentationLinks


# What a doc too sensitive to share leaves blank: where it lives and its notes.
_WITHHELD = ("obsidian_path", "github_path", "external_url", "notes")


def serialize_documentation(
    record: DocumentationRecord, *, relationships: bool = True, principal: Principal | None = None
) -> dict[str, Any]:
    """``record`` as ``principal`` may read it; None is an AI-facing surface's reader."""

    shown = record.is_safe_for_ai_export or reads_whole(principal)
    withheld = {} if shown else dict.fromkeys(_WITHHELD, "")
    fields = row(record, DocumentationRow, system=record.system_service, **withheld)
    if relationships:
        fields["url"] = entity_link(Kind.DOCUMENT, record.doc_id).url
        fields["relationships"] = {
            "projects": list(record.related_projects.order_by("slug").values_list("slug", flat=True)),
            "assets": list(record.related_assets.order_by("slug").values_list("slug", flat=True)),
            "expense_ids": list(record.related_expenses.order_by("id").values_list("id", flat=True)),
        }
    return fields


def list_documentation(*, limit: int = 50, principal: Principal | None = None) -> dict[str, Any]:
    """The documentation records ``principal`` may read (``sensitivity``)."""

    return listing(
        readable_records(principal),
        partial(serialize_documentation, relationships=False, principal=principal),
        order=(),
        limit=limit,
    )


def get_documentation(doc_id: str, *, principal: Principal | None = None) -> dict[str, Any]:
    """One documentation record ``principal`` may read, by its doc ID, with the
    records it is linked to; one it may not is answered as none, as the list
    leaves it out."""

    try:
        record = readable_records(principal).get(doc_id=doc_id)
    except DocumentationRecord.DoesNotExist as exc:
        raise RecordNotFound("No documentation record has that doc ID.") from exc
    return serialize_documentation(record, principal=principal)


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
    Describe("Docs created, updated and skipped, links to records HQ does not hold, and orphans found or pruned."),
]


class DocumentationSync(TypedDict):
    """What syncing the vault's docs into HQ did."""

    stats: SyncStats


@final
class DocumentationSynced(DocumentationSync):
    """What syncing the vault's docs into HQ did, as the command answers it."""

    ok: Literal[True]
    message: CallMessage


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

    if prune_orphans:
        principal.require(Capability.PRUNE_DOCUMENTATION)
    if len(manifest) > MAX_MANIFEST_ITEMS:
        raise PolicyError(f"Manifest exceeds the {MAX_MANIFEST_ITEMS}-record safety limit.")
    if any(not isinstance(entry, dict) for entry in manifest):
        raise PolicyError("Every manifest record must be a JSON object.")
    problems = validate_manifest_data(manifest)
    if problems:
        return refuse(Code.INVALID_INPUT, f"{counted(len(problems), 'record')} refused. Nothing was synced.", problems)
    if prune_orphans and not confirm_prune:
        raise PolicyError("confirm_prune must be true when prune_orphans is true")

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
    said = f"Manifest imported: {stats['created']} new, {stats['updated']} changed, {stats['skipped']} unchanged."
    return {"ok": True, "stats": stats, "message": said}


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
