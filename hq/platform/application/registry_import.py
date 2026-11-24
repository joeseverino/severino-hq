"""One document of projects and assets, imported in one transaction.

For facts no connection knows. Every record goes through the same upsert use
cases the single-record capabilities use, so validation, persistence and
attribution are theirs. What it checks, keeps and reports, and why a derived
value wins over an imported one: docs/Derived-Facts.md#one-time-import.
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Annotated, Any, Literal, TypedDict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from pydantic import ValidationError as PydanticValidationError

from hq.domains.projects.models import Project
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog

from .answer_fields import RecordSlug
from .assets import AssetCommand
from .command_fields import Describe
from .describe import listed
from .domains import records_of
from .host_sections import load
from .integration_specs import command_adapter
from .projects import ProjectCommand
from .record_declaration import Records
from .record_errors import RecordConflict, RecordNotFound
from .refusal_codes import Code
from .refusal_status import RefusalEnvelope, refuse
from .security import Principal
from .stored_columns import laid_over
from .ui import counted, ended

MAX_IMPORT_RECORDS = 1000

# Fields a connection may derive, per record type. The import never replaces a
# stored value of one of these with a different value.
DERIVED_FIELDS: dict[str, tuple[str, ...]] = {
    "project": ("public_url",),
    "asset": (),
}

REQUIRED_CAPABILITIES = (records_of("projects").write, records_of("assets").write)


# A record of the document is laid over the one stored under its slug, so
# every field but the slug may be left out.
ImportedProject = laid_over(
    ProjectCommand,
    name="ImportedProject",
    doc="A project to import: its slug, and the fields to lay over the project stored under it.",
    key="slug",
    key_type=RecordSlug,
)
ImportedAsset = laid_over(
    AssetCommand,
    name="ImportedAsset",
    doc="An asset to import: its slug, and the fields to lay over the asset stored under it.",
    key="slug",
    key_type=RecordSlug,
)


@dataclass(frozen=True, slots=True)
class HQImportCommand:
    projects: Annotated[list[ImportedProject], Describe("Projects to import, each laid over the one of its slug.")] = (
        field(default_factory=list)
    )
    assets: Annotated[list[ImportedAsset], Describe("Assets to import, each laid over the one of its slug.")] = field(
        default_factory=list
    )
    check_only: Annotated[bool, Describe("Check the import without saving anything.")] = False


_PROJECT = records_of("projects")
_ASSET = records_of("assets")


@dataclass
class _Planned:
    kind: Records
    index: int
    slug: str
    command: Any
    outcome: str  # created, updated or unchanged
    kept: list[dict[str, str]]


# What a record's error depends on, each the code a refusal of it alone
# answers, in the order a document's refusal takes the first one it holds: the
# document itself, a record it names that is not stored, or the stored record
# it lays over (one it creates lacks what creating one needs).
CAUSES = (Code.INVALID_INPUT, Code.NOT_FOUND, Code.PRECONDITION, RecordConflict.code)

# One error of a record: its cause and its words.
type _Error = tuple[Code, str]


@dataclass(frozen=True, slots=True)
class _Problem:
    cause: Code
    detail: dict[str, Any]


class _Refused(Exception):
    def __init__(self, problem: _Problem):
        super().__init__(problem.detail.get("errors"))
        self.problem = problem


def _problem(kind: str, index: int, slug: str, *errors: _Error) -> _Problem:
    cause = min((cause for cause, _ in errors), key=CAUSES.index)
    return _Problem(cause, {"kind": kind, "index": index, "slug": slug, "errors": [said for _, said in errors]})


def _refused(message: str, problems: list[_Problem]) -> dict[str, Any]:
    """An import refused, each record's problems its details, under the code
    of the first cause any of them holds."""

    return refuse(
        min((problem.cause for problem in problems), key=CAUSES.index),
        message,
        [problem.detail for problem in problems],
    )


def _stored(kind: Records, instance) -> dict[str, Any]:
    """An existing row as the command's fields."""

    values = {}
    for item in fields(load(kind.command)):
        if item.name == "related_projects":
            values[item.name] = tuple(sorted(instance.related_projects.values_list("slug", flat=True)))
        else:
            values[item.name] = getattr(instance, item.name)
    return values


def _existing(kind: Records, records: list[Any]) -> dict[str, Any]:
    slugs = [record["slug"].strip() for record in records]
    query = load(kind.model).objects.filter(slug__in=slugs)
    if kind.noun == "asset":
        query = query.prefetch_related("related_projects")
    return {row.slug: row for row in query}


def _slug_errors(slug: str, seen: set[str]) -> list[_Error]:
    """A record's slug errors; records the slug as seen."""

    errors = [(Code.INVALID_INPUT, "slug appears twice in this document.")] if slug in seen else []
    seen.add(slug)
    return errors


def _keep_derived(
    kind: Records, record: dict, stored: dict[str, Any] | None, values: dict[str, Any]
) -> list[dict[str, str]]:
    """Hold stored derived fields against an offered change; the kept pairs."""

    kept = []
    if stored is None:
        return kept
    for name in DERIVED_FIELDS[kind.noun]:
        if name in record and stored[name] and record[name] != stored[name]:
            kept.append({"field": name, "kept": str(stored[name]), "offered": str(record[name])})
            values[name] = stored[name]
    return kept


def _command(kind: Records, values: dict[str, Any]) -> tuple[Any, list[_Error]]:
    """The record laid over what is stored as its command. Each field already
    holds as the command holds it, so what fails is what the stored record
    lacks: every field a record created needs."""

    lacking = f"HQ has no {kind.noun} of this slug to lay it over."
    try:
        return command_adapter(load(kind.command)).validate_python(values), []
    except PydanticValidationError as exc:
        return None, [
            (
                Code.PRECONDITION,
                f"{'.'.join(str(part) for part in error['loc']) or 'record'}: {error['msg']}; {lacking}",
            )
            for error in exc.errors()
        ]


def _outcome(command: Any, stored: dict[str, Any] | None) -> str:
    if stored is None:
        return "created"
    proposed = asdict(command)
    if "related_projects" in proposed:
        proposed["related_projects"] = tuple(sorted(proposed["related_projects"]))
    return "unchanged" if proposed == stored else "updated"


def _plan_record(
    kind: Records,
    index: int,
    record: dict,
    slug: str,
    row: Any,
    known_projects: set[str],
) -> tuple[_Planned | None, list[_Error]]:
    stored = _stored(kind, row) if row is not None else None
    values = {**(stored or {}), **record}
    kept = _keep_derived(kind, record, stored, values)
    command, errors = _command(kind, values)
    if errors:
        return None, errors
    errors = _model_errors(kind, command, known_projects)
    if errors:
        return None, errors
    return _Planned(kind, index, slug, command, _outcome(command, stored), kept), []


def _plan_kind(
    kind: Records,
    records: list[Any],
    problems: list[_Problem],
    known_projects: set[str],
) -> list[_Planned]:
    existing = _existing(kind, records)
    seen: set[str] = set()
    planned = []
    for index, record in enumerate(records):
        slug = record["slug"].strip()
        errors = _slug_errors(slug, seen)
        if not errors:
            plan, errors = _plan_record(kind, index, record, slug, existing.get(slug), known_projects)
        if errors:
            problems.append(_problem(kind.noun, index, slug, *errors))
            continue
        planned.append(plan)
    return planned


def _model_errors(kind: Records, command: Any, known_projects: set[str]) -> list[_Error]:
    """Model validation of one record, without writing it."""

    values = asdict(command)
    related = values.pop("related_projects", ())
    errors: list[_Error] = []
    missing = sorted(set(related) - known_projects)
    if missing:
        errors.append(
            (
                Code.NOT_FOUND,
                ended(
                    f"{counted(len(missing), 'related project not found', 'related projects not found')}: "
                    f"{', '.join(missing)}"
                ),
            )
        )
    try:
        load(kind.model)(**values).full_clean(validate_unique=False)
    except DjangoValidationError as exc:
        errors.extend(_said(exc))
    return errors


def _said(exc: DjangoValidationError) -> list[_Error]:
    """Each error of a model's validation, a reference to nothing stored told from an invalid value."""

    return [
        (Code.NOT_FOUND if error.code == Code.NOT_FOUND else Code.INVALID_INPUT, f"{name}: {message}")
        for name, errors in exc.error_dict.items()
        for error in errors
        for message in error.messages
    ]


def _apply(planned: _Planned, principal: Principal) -> None:
    # An unchanged record is not saved: a save moves updated_at, which is the
    # default sort and the expected_updated_at token.
    if planned.outcome == "unchanged":
        _audit(planned, planned.slug)
        return
    try:
        result = load(planned.kind.upsert)(planned.command, principal=principal)
    except DjangoValidationError as exc:
        errors = (
            _said(exc) if hasattr(exc, "error_dict") else [(Code.INVALID_INPUT, message) for message in exc.messages]
        )
        raise _Refused(_problem(planned.kind.noun, planned.index, planned.slug, *errors)) from exc
    except (RecordNotFound, RecordConflict) as exc:
        raise _Refused(_problem(planned.kind.noun, planned.index, planned.slug, (exc.code, str(exc)))) from exc
    _audit(planned, result[planned.kind.noun]["slug"])


def _audit(planned: _Planned, slug: str) -> None:
    record_event(
        action=AuditLog.Action.IMPORTED,
        obj=load(planned.kind.model).objects.get(slug=slug),
        type_label=load(planned.kind.model).__name__,
        message=ended(f"Imported {planned.kind.noun} {planned.slug}: {planned.outcome}"),
        metadata={"outcome": planned.outcome, "kept": planned.kept},
        required=True,
    )


def _report(planned: list[_Planned]) -> list[dict[str, Any]]:
    return [{"slug": item.slug, "outcome": item.outcome, "kept": item.kept} for item in planned]


# What the import does with each record of the document.
OUTCOMES = ("created", "updated", "unchanged")


class KeptValue(TypedDict):
    """A stored value the import kept in place of the one the document offered."""

    field: Annotated[str, Describe("The field whose stored value was kept.")]
    kept: Annotated[str, Describe("The value HQ kept, as text.")]
    offered: Annotated[str, Describe("The value the document offered, as text.")]


class ImportedRecord(TypedDict):
    """One record of the document, and what the import does with it."""

    slug: RecordSlug
    outcome: Annotated[str, Describe(ended(f"What the import does with it: {listed(OUTCOMES)}"))]
    kept: Annotated[list[KeptValue], Describe("The stored values it keeps in place of the ones offered.")]


class ImportSummary(TypedDict):
    """How many records the import creates, updates and leaves as they are, and how many values it keeps."""

    created: Annotated[int, Describe("How many records it creates.")]
    updated: Annotated[int, Describe("How many records it updates.")]
    unchanged: Annotated[int, Describe("How many records it leaves as they are.")]
    kept: Annotated[int, Describe("How many stored values it keeps in place of the ones offered.")]


class HqImported(TypedDict):
    """What importing the document did, or would do where it was only checked."""

    ok: Literal[True]
    check_only: Annotated[bool, Describe("Whether the import was only checked, so nothing was saved.")]
    projects: Annotated[list[ImportedRecord], Describe("Each project in the document, in its order.")]
    assets: Annotated[list[ImportedRecord], Describe("Each asset in the document, in its order.")]
    summary: ImportSummary


def import_registry(command: HQImportCommand, *, principal: Principal) -> HqImported | RefusalEnvelope:
    """Validate the whole document, then upsert every record or none."""

    total = len(command.projects) + len(command.assets)
    if total > MAX_IMPORT_RECORDS:
        limit = f"The document holds {counted(total, 'record')}; the limit is {MAX_IMPORT_RECORDS}."
        return _refused(limit, [_problem("document", 0, "", (Code.INVALID_INPUT, limit))])

    problems: list[_Problem] = []
    projects = _plan_kind(_PROJECT, command.projects, problems, set())
    known = {item.slug for item in projects} | set(Project.objects.values_list("slug", flat=True))
    assets = _plan_kind(_ASSET, command.assets, problems, known)
    if problems:
        return _refused(f"{counted(len(problems), 'record')} refused. Nothing was imported.", problems)

    summary = {outcome: sum(item.outcome == outcome for item in (*projects, *assets)) for outcome in OUTCOMES}
    summary["kept"] = sum(len(item.kept) for item in (*projects, *assets))
    result = {
        "ok": True,
        "check_only": command.check_only,
        "projects": _report(projects),
        "assets": _report(assets),
        "summary": summary,
    }
    try:
        with transaction.atomic():
            for planned in (*projects, *assets):
                _apply(planned, principal)
            record_event(
                action=AuditLog.Action.IMPORTED,
                type_label="Registry",
                message=(f"Imported {counted(len(projects), 'project')} and {counted(len(assets), 'asset')}."),
                metadata={"summary": summary},
                required=True,
            )
            if command.check_only:
                transaction.set_rollback(True)
    except _Refused as exc:
        return _refused("1 record was refused while writing. Nothing was imported.", [exc.problem])
    return result


def execute_hq_import(
    command: HQImportCommand,
    *,
    principal: Principal,
) -> HqImported | RefusalEnvelope:
    return import_registry(command, principal=principal)
