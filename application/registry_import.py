"""One document of projects and assets, imported in one transaction.

For facts no connection knows. Every record goes through the same upsert use
cases the single-record capabilities use, so validation, persistence and
attribution are theirs. The whole document is checked before anything is
written, and a record refused while writing rolls every record back.

Idempotent by slug. A field a record leaves out keeps its stored value.

Derived fields: a connection can derive ``Project.public_url``. The import sets
it only where the stored value is blank or equal; a different stored value is
kept and reported, never overwritten. See docs/DERIVED_FACTS.md.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from assets.models import Asset
from core.audit import operation_context, record_event
from core.models import AuditLog
from projects.models import Project

from . import assets as asset_use_cases
from . import projects as project_use_cases
from .assets import AssetCommand, upsert_asset
from .projects import ProjectCommand, upsert_project
from .security import Capability, Principal
from .ui import counted

MAX_IMPORT_RECORDS = 1000

# Fields a connection may derive, per record type. The import never replaces a
# stored value of one of these with a different value.
DERIVED_FIELDS: dict[str, tuple[str, ...]] = {
    "project": ("public_url",),
    "asset": (),
}

REQUIRED_CAPABILITIES = (Capability.WRITE_PROJECTS, Capability.WRITE_ASSETS)


@dataclass(frozen=True)
class HQImportCommand:
    projects: list[dict[str, Any]] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    check_only: bool = False


@dataclass(frozen=True)
class _Kind:
    name: str
    model: type
    command: type
    upsert: Any
    use_case_errors: tuple[type[Exception], ...]


_PROJECT = _Kind(
    "project",
    Project,
    ProjectCommand,
    upsert_project,
    (project_use_cases.NotFoundError, project_use_cases.ConflictError),
)
_ASSET = _Kind(
    "asset",
    Asset,
    AssetCommand,
    upsert_asset,
    (asset_use_cases.NotFoundError, asset_use_cases.ConflictError),
)


@dataclass
class _Planned:
    kind: _Kind
    index: int
    slug: str
    command: Any
    outcome: str  # created, updated or unchanged
    kept: list[dict[str, str]]


class _Refused(Exception):
    def __init__(self, problem: dict[str, Any]):
        super().__init__(problem.get("errors"))
        self.problem = problem


def _problem(kind: str, index: int, slug: str, *errors: str) -> dict[str, Any]:
    return {"kind": kind, "index": index, "slug": slug, "errors": list(errors)}


def _stored(kind: _Kind, instance) -> dict[str, Any]:
    """An existing row as the command's fields."""

    values = {}
    for item in fields(kind.command):
        if item.name == "related_projects":
            values[item.name] = tuple(
                sorted(instance.related_projects.values_list("slug", flat=True))
            )
        else:
            values[item.name] = getattr(instance, item.name)
    return values


def _existing(kind: _Kind, records: list[Any]) -> dict[str, Any]:
    slugs = [
        str(record.get("slug", "")).strip() for record in records if isinstance(record, dict)
    ]
    query = kind.model.objects.filter(slug__in=slugs)
    if kind is _ASSET:
        query = query.prefetch_related("related_projects")
    return {row.slug: row for row in query}


def _shape_errors(record: dict, slug: str, seen: set[str], names: set[str]) -> list[str]:
    """A record's slug and field-name errors; records the slug as seen."""

    errors = []
    if not slug:
        errors.append("slug is required.")
    elif slug in seen:
        errors.append("slug appears twice in this document.")
    seen.add(slug)
    unknown = sorted(set(record) - names)
    if unknown:
        errors.append(f"Unknown fields: {', '.join(unknown)}.")
    return errors


def _keep_derived(
    kind: _Kind, record: dict, stored: dict[str, Any] | None, values: dict[str, Any]
) -> list[dict[str, str]]:
    """Hold stored derived fields against an offered change; the kept pairs."""

    kept = []
    if stored is None:
        return kept
    for name in DERIVED_FIELDS[kind.name]:
        if name in record and stored[name] and record[name] != stored[name]:
            kept.append({"field": name, "kept": str(stored[name]), "offered": str(record[name])})
            values[name] = stored[name]
    return kept


def _command(kind: _Kind, values: dict[str, Any]) -> tuple[Any, list[str]]:
    try:
        return TypeAdapter(kind.command).validate_python(values), []
    except PydanticValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc']) or 'record'}: {error['msg']}"
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
    kind: _Kind,
    index: int,
    record: dict,
    slug: str,
    row: Any,
    known_projects: set[str],
) -> tuple[_Planned | None, list[str]]:
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
    kind: _Kind,
    records: list[Any],
    problems: list[dict[str, Any]],
    known_projects: set[str],
) -> list[_Planned]:
    names = {item.name for item in fields(kind.command)}
    existing = _existing(kind, records)
    seen: set[str] = set()
    planned = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            problems.append(_problem(kind.name, index, "", "Each record is a JSON object."))
            continue
        slug = str(record.get("slug", "")).strip()
        errors = _shape_errors(record, slug, seen, names)
        if not errors:
            plan, errors = _plan_record(
                kind, index, record, slug, existing.get(slug), known_projects
            )
        if errors:
            problems.append(_problem(kind.name, index, slug, *errors))
            continue
        planned.append(plan)
    return planned


def _model_errors(kind: _Kind, command: Any, known_projects: set[str]) -> list[str]:
    """Model validation of one record, without writing it."""

    values = asdict(command)
    related = values.pop("related_projects", ())
    errors = []
    missing = sorted(set(related) - known_projects)
    if missing:
        errors.append(
            f"{counted(len(missing), 'related project not found', 'related projects not found')}: "
            f"{', '.join(missing)}."
        )
    try:
        kind.model(**values).full_clean(validate_unique=False)
    except DjangoValidationError as exc:
        errors.extend(
            f"{name}: {message}"
            for name, messages in exc.message_dict.items()
            for message in messages
        )
    return errors


def _apply(planned: _Planned, principal: Principal) -> None:
    # An unchanged record is not saved: a save moves updated_at, which is the
    # default sort and the expected_updated_at token.
    if planned.outcome == "unchanged":
        _audit(planned, planned.slug)
        return
    try:
        result = planned.kind.upsert(planned.command, principal=principal)
    except DjangoValidationError as exc:
        raise _Refused(
            _problem(planned.kind.name, planned.index, planned.slug, *exc.messages)
        ) from exc
    except planned.kind.use_case_errors as exc:
        raise _Refused(
            _problem(planned.kind.name, planned.index, planned.slug, str(exc))
        ) from exc
    _audit(planned, result[planned.kind.name]["slug"])


def _audit(planned: _Planned, slug: str) -> None:
    record_event(
        action=AuditLog.Action.IMPORTED,
        obj=planned.kind.model.objects.get(slug=slug),
        type_label=planned.kind.model.__name__,
        message=f"Imported {planned.kind.name} {planned.slug}: {planned.outcome}.",
        metadata={"outcome": planned.outcome, "kept": planned.kept},
        required=True,
    )


def _report(planned: list[_Planned]) -> list[dict[str, Any]]:
    return [
        {"slug": item.slug, "outcome": item.outcome, "kept": item.kept} for item in planned
    ]


def import_registry(
    command: HQImportCommand, *, principal: Principal
) -> dict[str, Any]:
    """Validate the whole document, then upsert every record or none."""

    for capability in REQUIRED_CAPABILITIES:
        principal.require(capability)
    total = len(command.projects) + len(command.assets)
    if total > MAX_IMPORT_RECORDS:
        return {
            "ok": False,
            "problems": [
                _problem(
                    "document",
                    0,
                    "",
                    f"The document holds {counted(total, 'record')}; "
                    f"the limit is {MAX_IMPORT_RECORDS}.",
                )
            ],
        }

    problems: list[dict[str, Any]] = []
    projects = _plan_kind(_PROJECT, command.projects, problems, set())
    known = {item.slug for item in projects} | set(
        Project.objects.values_list("slug", flat=True)
    )
    assets = _plan_kind(_ASSET, command.assets, problems, known)
    if problems:
        return {"ok": False, "problems": problems}

    summary = {
        outcome: sum(item.outcome == outcome for item in (*projects, *assets))
        for outcome in ("created", "updated", "unchanged")
    }
    summary["kept"] = sum(len(item.kept) for item in (*projects, *assets))
    result = {
        "ok": True,
        "check_only": command.check_only,
        "projects": _report(projects),
        "assets": _report(assets),
        "summary": summary,
    }
    try:
        with transaction.atomic(), operation_context(
            interface=principal.interface, actor=principal.actor, operation="hq.import"
        ):
            for planned in (*projects, *assets):
                _apply(planned, principal)
            record_event(
                action=AuditLog.Action.IMPORTED,
                type_label="Registry",
                message=(
                    f"Imported {counted(len(projects), 'project')} and "
                    f"{counted(len(assets), 'asset')}."
                ),
                metadata={"summary": summary},
                required=True,
            )
            if command.check_only:
                transaction.set_rollback(True)
    except _Refused as exc:
        return {"ok": False, "problems": [exc.problem]}
    return result


def execute_hq_import(
    command: HQImportCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    return import_registry(command, principal=principal)
