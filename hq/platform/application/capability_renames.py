"""The migration operation that moves stored rows to a capability's new name.

A standing rule left under an old name matches nothing, so its call would run
at the default: a deny or an approve would quietly loosen. A pending approval
under an old name could no longer be approved, and its fingerprint covers the
name, so it is computed again. A settled approval is history and keeps the
name it was decided under. The host's renames and each extension's run
through this one operation (``hq_sdk.migrations``).
"""

from collections.abc import Callable, Mapping
from typing import Any

from django.db import migrations

# Loaded once a migration runs, when the apps are.
lazy from .approvals import fingerprint

# The names a handler's own binding once gave the rows it wrote, before the
# runner alone named them: each is read as the capability that records the
# act now, by that capability's title.
RECORDED_AS = {
    "certificate.renew.request": "certificate.renew",
    "infrastructure.reconcile.request": "infrastructure.resource.reconcile",
    "infrastructure.resource.forget": "infrastructure.resource.remove",
    "infrastructure.restart.request": "infrastructure.resource.restart",
    "infrastructure.start.request": "infrastructure.resource.start",
    "infrastructure.stop.request": "infrastructure.resource.stop",
    "project.refresh_content": "project.refresh",
    "tailnet.routes.approve.request": "tailnet.routes.approve",
}

# The host migration whose state the operation reads. A migration using the
# operation depends on it. It never moves once released: Django refuses a
# history where an applied migration's dependency was applied after it.
CAPABILITY_ROWS = ("control_plane", "0039_capability_names_follow_the_pattern")


def _moving(renamed: Mapping[str, str]) -> Callable[[Any, Any], None]:
    def move(apps: Any, _schema_editor: Any) -> None:
        rules = apps.get_model("control_plane", "CapabilityRule").objects
        held = apps.get_model("control_plane", "ApprovalRequest").objects
        for old, new in renamed.items():
            rules.filter(capability=old).update(capability=new)
            for pending in held.filter(capability=old, state="pending"):
                pending.capability = new
                pending.content_fingerprint = fingerprint(new, pending.target, pending.payload, pending.baseline)
                pending.save(update_fields=["capability", "content_fingerprint"])

    return move


class RenameCapabilities(migrations.RunPython):
    """Move each standing rule and pending approval from an old capability name (a key) to its new one."""

    def __init__(self, renamed: Mapping[str, str]) -> None:
        self.renamed = dict(renamed)
        super().__init__(_moving(self.renamed), _moving({new: old for old, new in self.renamed.items()}))
