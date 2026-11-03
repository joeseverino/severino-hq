"""The vault's documentation, brought into HQ as one transaction.

Documentation only. HQ derives its picture of the infrastructure from what its
credentials reach, so no document describes it.
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

from django.db import transaction

from .command_fields import Describe
from .documentation import (
    ConfirmPrune,
    DocumentationSync,
    ManifestEntry,
    PruneOrphans,
    UpdateExisting,
    sync_documentation,
)
from .refusal_status import RefusalEnvelope, refused
from .security import Principal


@dataclass(frozen=True, slots=True)
class HQSyncCommand:
    manifest: Annotated[list[ManifestEntry], Describe("The records to sync, one per entry.")]
    update_existing: UpdateExisting = True
    report_orphans: Annotated[bool, Describe("Report records the manifest no longer lists.")] = True
    prune_orphans: PruneOrphans = False
    confirm_prune: ConfirmPrune = False


class HqSynced(TypedDict):
    """What syncing HQ from the vault did, each part under its own name."""

    ok: Literal[True]
    documentation: DocumentationSync


@transaction.atomic
def execute_hq_sync(
    command: HQSyncCommand,
    *,
    principal: Principal,
) -> HqSynced | RefusalEnvelope:
    """Apply the whole manifest or none of it."""

    documentation = sync_documentation(
        command.manifest,
        principal=principal,
        update_existing=command.update_existing,
        report_orphans=command.report_orphans,
        prune_orphans=command.prune_orphans,
        confirm_prune=command.confirm_prune,
    )
    if refused(documentation):
        transaction.set_rollback(True)
        return documentation
    return {"ok": True, "documentation": {"stats": documentation["stats"]}}
