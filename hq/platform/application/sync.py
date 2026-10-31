"""The vault's documentation, brought into HQ as one transaction.

Documentation only. HQ derives its picture of the infrastructure from what its
credentials reach, so no document describes it.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction

from .command_fields import Describe
from .documentation import sync_documentation
from .security import Principal


@dataclass(frozen=True, slots=True)
class HQSyncCommand:
    manifest: Annotated[list[dict[str, Any]], Describe("The records to sync, one per entry.")]
    update_existing: Annotated[bool, Describe("Update records HQ already holds.")] = True
    report_orphans: Annotated[bool, Describe("Report records the manifest no longer lists.")] = True
    prune_orphans: Annotated[bool, Describe("Delete records the manifest no longer lists.")] = False
    confirm_prune: Annotated[bool, Describe("Confirms the delete that prune_orphans asks for.")] = False


@transaction.atomic
def execute_hq_sync(
    command: HQSyncCommand,
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Apply the whole manifest or none of it."""

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
