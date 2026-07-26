"""Atomic synchronization boundary for HQ's external sources of truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from .documentation import sync_documentation
from .security import Principal
from .topology import sync_topology


@dataclass(frozen=True)
class HQSyncCommand:
    manifest: list[dict[str, Any]]
    topology: dict[str, Any]
    update_existing: bool = True
    report_orphans: bool = True
    prune_orphans: bool = False
    confirm_prune: bool = False


@transaction.atomic
def execute_hq_sync(
    command: HQSyncCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Apply documentation and topology together or apply neither."""

    del expected_updated_at
    documentation = sync_documentation(
        command.manifest,
        principal=principal,
        update_existing=command.update_existing,
        report_orphans=command.report_orphans,
        prune_orphans=command.prune_orphans,
        confirm_prune=command.confirm_prune,
    )
    if not documentation["ok"]:
        transaction.set_rollback(True)
        return documentation
    topology = sync_topology(command.topology, principal=principal)
    return {
        "ok": True,
        "documentation": documentation,
        "topology": topology,
    }
