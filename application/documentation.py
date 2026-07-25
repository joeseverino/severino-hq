"""Documentation synchronization use case shared by web, MCP, and CLI."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from django.db import transaction

from core.audit import operation_context, record_event
from core.models import AuditLog
from docs_index.importer import (
    ManifestImportError,
    import_manifest_data,
    validate_manifest_data,
)
from docs_index.models import DocumentationRecord
from .security import Capability, Principal
from assets.models import Asset
from expenses.models import Expense
from projects.models import Project

MAX_MANIFEST_ITEMS = 2000


@dataclass(frozen=True)
class DocumentationSyncCommand:
    manifest: list[dict[str, Any]]
    update_existing: bool = True
    report_orphans: bool = False
    prune_orphans: bool = False
    confirm_prune: bool = False


@dataclass(frozen=True)
class DocumentationCommand:
    doc_id: str
    title: str
    doc_type: str = "runbook"
    system_service: str = ""
    environment: str = "other"
    status: str = "draft"
    sensitivity: str = "internal"
    obsidian_path: str = ""
    github_path: str = ""
    external_url: str = ""
    last_reviewed: date | None = None
    published_at: date | None = None
    notes: str = ""
    related_projects: tuple[str, ...] = ()
    related_assets: tuple[str, ...] = ()
    related_expenses: tuple[int, ...] = ()


def serialize_documentation(record: DocumentationRecord) -> dict[str, Any]:
    safe = record.is_safe_for_ai_export
    return {
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
        "last_reviewed": (
            record.last_reviewed.isoformat() if record.last_reviewed else None
        ),
        "published_at": (
            record.published_at.isoformat() if record.published_at else None
        ),
        "notes": record.notes if safe else "",
        "updated_at": record.updated_at.isoformat(),
        "relationships": {
            "projects": list(
                record.related_projects.order_by("slug").values_list("slug", flat=True)
            ),
            "assets": list(
                record.related_assets.order_by("slug").values_list("slug", flat=True)
            ),
            "expense_ids": list(
                record.related_expenses.order_by("id").values_list("id", flat=True)
            ),
        },
    }


def _resolve(model, field, values, label):
    records = list(model.objects.filter(**{f"{field}__in": values}))
    found = {getattr(record, field) for record in records}
    missing = sorted(set(values) - found)
    if missing:
        raise ManifestImportError(f"Related {label}(s) not found: {missing}")
    return records


@transaction.atomic
def save_documentation(
    command: DocumentationCommand,
    *,
    principal: Principal,
    current_doc_id: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    principal.require(Capability.WRITE_DOCUMENTATION)
    operation = (
        "documentation.create" if current_doc_id is None else "documentation.update"
    )
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        if current_doc_id is None:
            record, created = DocumentationRecord(), True
        else:
            try:
                record = DocumentationRecord.objects.select_for_update().get(
                    doc_id=current_doc_id
                )
            except DocumentationRecord.DoesNotExist as exc:
                raise ManifestImportError(
                    f"Documentation record {current_doc_id!r} was not found."
                ) from exc
            created = False
            if expected_updated_at and record.updated_at.isoformat() != expected_updated_at:
                raise ManifestImportError(
                    f"Documentation record {current_doc_id!r} changed after it was read."
                )
        values = asdict(command)
        projects = _resolve(
            Project, "slug", values.pop("related_projects"), "project"
        )
        assets = _resolve(Asset, "slug", values.pop("related_assets"), "asset")
        expenses = _resolve(
            Expense, "id", values.pop("related_expenses"), "expense"
        )
        for field, value in values.items():
            setattr(record, field, value)
        record.full_clean()
        record.save()
        record.related_projects.set(projects)
        record.related_assets.set(assets)
        record.related_expenses.set(expenses)
    return {
        "ok": True,
        "created": created,
        "documentation": serialize_documentation(record),
    }


def documentation_command_from_cleaned_data(data) -> DocumentationCommand:
    return DocumentationCommand(
        **{
            field: data.get(field)
            for field in DocumentationCommand.__dataclass_fields__
            if not field.startswith("related_")
        },
        related_projects=tuple(row.slug for row in data["related_projects"]),
        related_assets=tuple(row.slug for row in data["related_assets"]),
        related_expenses=tuple(row.id for row in data["related_expenses"]),
    )


def sync_documentation(
    manifest: list[dict[str, Any]],
    *,
    principal: Principal,
    update_existing: bool = True,
    report_orphans: bool = False,
    prune_orphans: bool = False,
    confirm_prune: bool = False,
) -> dict[str, Any]:
    """Validate and atomically synchronize vault metadata into HQ."""

    principal.require(Capability.SYNC_DOCUMENTATION)
    if prune_orphans:
        principal.require(Capability.PRUNE_DOCUMENTATION)
    if len(manifest) > MAX_MANIFEST_ITEMS:
        raise ManifestImportError(
            f"Manifest exceeds the {MAX_MANIFEST_ITEMS}-record safety limit."
        )
    if any(not isinstance(entry, dict) for entry in manifest):
        raise ManifestImportError("Every manifest record must be a JSON object.")
    problems = validate_manifest_data(manifest)
    if problems:
        return {"ok": False, "problems": problems}
    if prune_orphans and not confirm_prune:
        raise ManifestImportError(
            "confirm_prune must be true when prune_orphans is true"
        )

    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="documentation.sync",
    ):
        stats = import_manifest_data(
            manifest,
            update_existing=update_existing,
            report_orphans=report_orphans or prune_orphans,
            prune_orphans=prune_orphans,
        )
        record_event(
            action=AuditLog.Action.IMPORTED,
            type_label="DocumentationRecord",
            message="Synchronized vault documentation manifest.",
            metadata={"stats": stats},
        )
    return {"ok": True, "stats": stats}


def execute_documentation_sync(
    command: DocumentationSyncCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    del expected_updated_at
    return sync_documentation(
        command.manifest,
        principal=principal,
        update_existing=command.update_existing,
        report_orphans=command.report_orphans,
        prune_orphans=command.prune_orphans,
        confirm_prune=command.confirm_prune,
    )
