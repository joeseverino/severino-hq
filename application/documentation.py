"""Documentation synchronization use case shared by web, MCP, and CLI."""

from __future__ import annotations

from typing import Any

from core.audit import operation_context, record_event
from core.models import AuditLog
from docs_index.importer import (
    ManifestImportError,
    import_manifest_data,
    validate_manifest_data,
)
from .security import Capability, Principal

MAX_MANIFEST_ITEMS = 2000


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
