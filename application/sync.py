"""The vault's documentation, brought into HQ as one transaction.

This used to carry an authored topology alongside it, so that HQ's picture of
the world and the vault's could not disagree. HQ derives that picture now -- from
what its credentials reach and what it has been told directly -- so there is
nothing here for a document to say.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from .documentation import sync_documentation
from .security import Principal


@dataclass(frozen=True)
class HQSyncCommand:
    manifest: list[dict[str, Any]]
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
    """Apply the whole manifest or none of it."""

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
    return {"ok": True, "documentation": documentation}
