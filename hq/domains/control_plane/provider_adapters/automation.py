"""The records automation binds to: one per adopted playbook, which its runs
bind to, and the source a Refresh scans for playbooks you adopt.

Declaration-only. HQ writes a playbook's record from the adopted catalog,
never from a form, so its spec is the playbook as data: what a run of it binds
to, and what a held run is measured against. Nothing sweeps a playbook, and
the controller runs none of it: HQ's runner claims each run.
"""

from typing import Any, Literal

from pydantic import Field

from hq.platform.application.automation_manifest import PLAYBOOK, PLAYBOOK_LONGEST, REVISION
from hq.platform.application.ui import ended
lazy from hq.platform.application.action_links import command_url
lazy from hq.platform.application.automation_runs import (
    RETRY_WORDS,
    access_words,
    anywhere,
    capability_name,
    operation_registry,
    resource_key,
)
lazy from hq.platform.application.labels import effect_label
lazy from hq.platform.application.routes import reverse

from ..provider_spec import ProviderModel, ProviderSpec, locked

OPERATION_KIND = "automation.operation"
SOURCE_KIND = "automation.source"

# What the operation's queued work is called (``OperationRequest.Action.RUN``).
RUN = "run"
# What a source's queued work is called (``OperationRequest.Action.SCAN``).
SCAN = "scan"


class OperationSpec(ProviderModel):
    """One adopted playbook: every field but its input type, which its schema's digest stands for."""

    operation: str = Field(min_length=1, max_length=160, title="Playbook ID")
    revision: str = Field(pattern=REVISION, title="Reviewed commit")
    # Inside the project the host's configuration names at that commit.
    playbook: str = Field(pattern=PLAYBOOK, max_length=PLAYBOOK_LONGEST, title="Playbook file")
    input_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", title="Input schema")
    target_kinds: list[str] = Field(min_length=1, title="Record types")
    hosts: list[str] = Field(default_factory=list, title="Machines")
    privilege: str = Field(default="", max_length=160, title="Privilege")
    effect: Literal["read", "infrastructure_change", "destructive"] = Field(title="Effect")
    timeout_seconds: int = Field(ge=1, le=3600, title="Timeout")
    # A read may assert nothing before or after it (``OperationManifest``).
    preconditions: list[str] = Field(default_factory=list, title="Preconditions")
    verification: list[str] = Field(default_factory=list, title="Verification")
    recovery: list[str] = Field(min_length=1, title="Recovery")
    retry: Literal["never", "observe_first", "safe"] = Field(title="Retry")


def _readout(spec: dict[str, Any], _status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was adopted. A run's own operation says what it found."""

    return (
        ("Reviewed commit", "", str(spec.get("revision", ""))),
        ("Playbook file", "", str(spec.get("playbook", ""))),
        ("Effect", "", effect_label(str(spec.get("effect", ""))) or str(spec.get("effect", ""))),
        ("Access", "", access_words(str(spec.get("privilege", "")))),
        ("Record types", "", ", ".join(spec.get("target_kinds", ()))),
        ("Machines", "", ", ".join(spec.get("hosts", ())) or anywhere(spec.get("target_kinds", ()))),
        ("Timeout", "", f"{spec.get('timeout_seconds', '')} seconds"),
        ("Retry", "", RETRY_WORDS.get(str(spec.get("retry", "")), str(spec.get("retry", "")))),
    )


def _named(resource: Any) -> str:
    """The adopted playbook whose record this is, or ""."""

    return next((operation for operation in operation_registry() if resource_key(operation) == resource.key), "")


def _home(resource: Any) -> str:
    """A playbook's command page while it is adopted, where everything about
    it is drawn; its record's own page once it is dropped."""

    operation = _named(resource)
    page = command_url(capability_name(operation)) if operation else ""
    return page or reverse("control_plane:detail", kwargs={"key": resource.key})


OPERATION = ProviderSpec(
    OPERATION_KIND,
    "An adopted playbook and its runs.",
    OperationSpec,
    actions={
        # The controller runs nothing of a playbook: a run is asked for by
        # the playbook's own command and claimed by HQ's runner.
        "reconcile": locked("Nothing to apply. Run the playbook from its own page."),
    },
    label="Playbook",
    declaration_only=True,
    hostnames=None,
    readout=_readout,
    home=_home,
    # Written from the adopted catalog while it names the playbook.
    sourced=lambda resource: bool(_named(resource)),
    key_hint=lambda spec: str(spec.get("operation", "")),
    name=lambda spec: str(spec.get("operation", "")),
    # It arrives with its adopted catalog, so nobody adds one from the picker.
    created_from="automation",
    removal_note=lambda spec: f"HQ forgets {ended(spec.get('operation', 'this playbook'))} A new run records it again.",
    unobserved_reason="Each run reports what it found.",
)


class SourceSpec(ProviderModel):
    """Where the source's repository is named: the host's configuration
    (``automation`` in its host.json), never HQ's, which holds no repository."""

    named_by: Literal["host"] = Field(default="host", title="Repository named by")


SOURCE = ProviderSpec(
    SOURCE_KIND,
    "The repository your playbooks come from. HQ scans it when you press Refresh.",
    SourceSpec,
    actions={
        # The controller scans nothing: a scan is asked for by Refresh and
        # claimed by HQ's runner, which holds the repository's token.
        "reconcile": locked("Nothing to apply. Press Refresh on Playbooks to scan it."),
    },
    label="Playbooks",
    declaration_only=True,
    hostnames=None,
    # The Playbooks page draws everything about the source: its scans, its
    # scan results and the playbooks adopted from it.
    home=lambda _resource: reverse("control_plane:procedures"),
    # HQ never reads the host's configuration, only what its record says: a
    # Refresh writes the one source, named by the host, so no person edits
    # or removes it by hand.
    sourced=lambda resource: resource.spec.get("named_by") == "host",
    created_from="automation",
    removal_note=lambda _spec: "HQ forgets its scan results. Refresh records them again.",
    unobserved_reason="Each Refresh reports what it found.",
)

DEFINITIONS = (OPERATION, SOURCE)
