"""The records automation binds to: one per procedure of the adopted catalog,
which its runs bind to, and the source a Refresh scans for the procedures it
declares and a person adopts them from.

Declaration-only. HQ writes a procedure's from the catalog adopted, never from
a form, so its spec is the procedure as data: what a run of it binds to, and
what a held run is measured against. Nothing sweeps a procedure, and the
controller runs none of it: a run is claimed by the automation worker.
"""

from typing import Any, Literal

from pydantic import Field

from hq.platform.application.automation_manifest import PLAYBOOK, PLAYBOOK_LONGEST, REVISION
from hq.platform.application.ui import ended

from ..provider_spec import ProviderModel, ProviderSpec, locked

OPERATION_KIND = "automation.operation"
SOURCE_KIND = "automation.source"

# What the operation's queued work is called (``OperationRequest.Action.RUN``).
RUN = "run"
# What a source's queued work is called (``OperationRequest.Action.SCAN``).
SCAN = "scan"


class OperationSpec(ProviderModel):
    """One adopted procedure, every field but its input type, which its schema's digest stands for."""

    operation: str = Field(min_length=1, max_length=160, title="Procedure")
    revision: str = Field(pattern=REVISION, title="Reviewed commit")
    # Inside the project the host's configuration names at that commit.
    playbook: str = Field(pattern=PLAYBOOK, max_length=PLAYBOOK_LONGEST, title="Playbook")
    input_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", title="Input schema")
    target_kinds: list[str] = Field(min_length=1, title="Acts on")
    hosts: list[str] = Field(default_factory=list, title="Hosts")
    privilege: str = Field(default="", max_length=160, title="Privilege profile")
    effect: Literal["read", "infrastructure_change", "destructive"] = Field(title="Effect")
    timeout_seconds: int = Field(ge=1, le=3600, title="Timeout")
    # A read may assert nothing before or after it (``OperationManifest``).
    preconditions: list[str] = Field(default_factory=list, title="Before it runs")
    verification: list[str] = Field(default_factory=list, title="Shows it worked")
    recovery: list[str] = Field(min_length=1, title="After a partial run")
    retry: Literal["never", "observe_first", "safe"] = Field(title="Retry")


def _readout(spec: dict[str, Any], _status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was adopted. A run's own operation says what it found."""

    return (
        ("Reviewed commit", "", str(spec.get("revision", ""))),
        ("Playbook", "", str(spec.get("playbook", ""))),
        ("Effect", "", str(spec.get("effect", ""))),
        ("Privilege profile", "", str(spec.get("privilege", "")) or "none"),
        ("Acts on", "", ", ".join(spec.get("target_kinds", ()))),
        ("Hosts", "", ", ".join(spec.get("hosts", ())) or "any of those"),
        ("Timeout", "", f"{spec.get('timeout_seconds', '')} seconds"),
        ("Retry", "", str(spec.get("retry", ""))),
    )


OPERATION = ProviderSpec(
    OPERATION_KIND,
    "An automation procedure of the adopted catalog, and the runs asked of it.",
    OperationSpec,
    actions={
        # The controller runs nothing of a procedure: a run is asked for by
        # the procedure's own command and claimed by the automation worker.
        "reconcile": locked("Nothing to apply: a procedure is run through its own command."),
    },
    label="Automation procedure",
    declaration_only=True,
    hostnames=None,
    readout=_readout,
    key_hint=lambda spec: str(spec.get("operation", "")),
    name=lambda spec: str(spec.get("operation", "")),
    # It arrives with its adopted catalog, so nobody adds one from the picker.
    created_from="automation",
    removal_note=lambda spec: (
        f"HQ forgets {ended(spec.get('operation', 'this procedure'))} Asking for a run records it again."
    ),
    unobserved_reason="Nothing to read: each run reports what it found.",
)


class SourceSpec(ProviderModel):
    """Where the source's repository is named: the host's configuration
    (``automation`` in its host.json), never HQ's, which holds no repository."""

    named_by: Literal["host"] = Field(default="host", title="Repository named by")


SOURCE = ProviderSpec(
    SOURCE_KIND,
    "The repository the automation worker's procedures come from, scanned when a person presses Refresh.",
    SourceSpec,
    actions={
        # The controller scans nothing: a scan is asked for by Refresh and
        # claimed by the automation worker, which holds the repository's token.
        "reconcile": locked("Nothing to apply: a source is read when someone presses Refresh."),
    },
    label="Automation source",
    declaration_only=True,
    hostnames=None,
    created_from="automation",
    removal_note=lambda _spec: "HQ forgets what its scans proposed. Refresh records it again.",
    unobserved_reason="Nothing to read on a timer: each Refresh reports what it found.",
)

DEFINITIONS = (OPERATION, SOURCE)
