"""The records automation binds to: one per adopted playbook, which its runs
bind to, and the source HQ scans for playbooks you adopt.

Declaration-only. HQ writes a playbook's record from the adopted catalog,
never from a form, so its spec is the playbook as data: what a run of it binds
to, and what a held run is measured against. Nothing sweeps a playbook, and
the controller runs none of it: HQ's runner claims each run.
"""

from typing import Annotated, Any, Literal

from pydantic import Field

from hq.platform.application.automation_manifest import PLAYBOOK, PLAYBOOK_LONGEST, REVISION, PlaybookEffect
from hq.platform.application.command_fields import NonEmpty, RequiredText
from hq.platform.application.contracts import Effect
from hq.platform.application.describe import Describe
from hq.platform.application.ui import ended
lazy from hq.platform.application.action_links import command_url
lazy from hq.platform.application.automation_runs import (
    RETRY_WORDS,
    access_words,
    capability_name,
    operation_registry,
    resource_key,
    timeout_words,
)
lazy from hq.platform.application.entity_links import page_url
lazy from hq.platform.application.playbook_reach import reachable
lazy from hq.platform.application.references import Kind
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

    operation: Annotated[RequiredText, Describe("", api="The playbook's id, as its sidecar names it.")] = Field(
        max_length=160, title="Playbook ID"
    )
    revision: Annotated[str, Describe("", api="The commit of the repository the playbook was reviewed at.")] = Field(
        pattern=REVISION, title="Reviewed commit"
    )
    # Inside the project the host's configuration names at that commit.
    playbook: Annotated[str, Describe("", api="The playbook's path in the repository.")] = Field(
        pattern=PLAYBOOK, max_length=PLAYBOOK_LONGEST, title="Playbook file"
    )
    input_schema_sha256: Annotated[str, Describe("", api="The SHA-256 digest of the playbook's input schema.")] = Field(
        pattern=r"^[0-9a-f]{64}$", title="Input schema"
    )
    target_kinds: Annotated[list[str], NonEmpty(), Describe("", api="The record types the playbook runs against.")] = (
        Field(title="Record types")
    )
    hosts: Annotated[
        list[str], Describe("", api="The machines the playbook may run on. Empty for any machine a target names.")
    ] = Field(default_factory=list, title="Machines")
    privilege: Annotated[str, Describe("", api="The privilege profile it runs with. Empty for none.")] = Field(
        default="", max_length=160, title="Privilege"
    )
    effect: Annotated[PlaybookEffect, Describe("", api="What running it does.")] = Field(title="Effect")
    timeout_seconds: Annotated[int, Describe("", api="How many seconds a run may take.")] = Field(
        ge=1, le=3600, title="Timeout"
    )
    # A read may assert nothing before or after it (``OperationManifest``).
    preconditions: Annotated[list[str], Describe("", api="What must hold before it runs.")] = Field(
        default_factory=list, title="Preconditions"
    )
    verification: Annotated[list[str], Describe("", api="What it checks once it has run.")] = Field(
        default_factory=list, title="Verification"
    )
    recovery: Annotated[list[str], NonEmpty(), Describe("", api="What to do when a run fails.")] = Field(
        title="Recovery"
    )
    retry: Annotated[
        Literal["never", "observe_first", "safe"],
        Describe("", api="Whether a run that did not finish may be run again."),
    ] = Field(title="Retry")


def _readout(spec: dict[str, Any], _status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was adopted. A run's own operation says what it found."""

    return (
        ("Reviewed commit", "", str(spec.get("revision", ""))),
        ("Playbook file", "", str(spec.get("playbook", ""))),
        ("Effect", "", Effect(spec["effect"]).label if spec.get("effect") in Effect else str(spec.get("effect", ""))),
        ("Access", "", access_words(str(spec.get("privilege", "")))),
        ("Record types", "", ", ".join(spec.get("target_kinds", ()))),
        (
            "Machines",
            "",
            ", ".join(reachable(adopted))
            if (adopted := operation_registry().get(str(spec.get("operation", ""))))
            else "",
        ),
        ("Timeout", "", timeout_words(spec.get("timeout_seconds"))),
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
    return page or page_url(Kind.RESOURCE, resource.key)


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

    named_by: Annotated[Literal["host"], Describe("", api="Where the repository is named.")] = Field(
        default="host", title="Repository named by"
    )


SOURCE = ProviderSpec(
    SOURCE_KIND,
    "The repository your playbooks come from. HQ scans it each time its default branch moves.",
    SourceSpec,
    actions={
        # The controller scans nothing: HQ asks for a scan when the
        # repository moves, and HQ's runner, which holds its token, claims it.
        "reconcile": locked("Nothing to apply. HQ scans it when its default branch moves."),
    },
    label="Playbooks",
    declaration_only=True,
    hostnames=None,
    # The Playbooks page draws everything about the source: its scans, its
    # scan results and the playbooks adopted from it.
    home=lambda _resource: reverse("control_plane:playbooks"),
    # HQ never reads the host's configuration, only what its record says: a
    # HQ writes the one source, named by the host, as it asks for its first
    # scan, so no person edits or removes it by hand.
    sourced=lambda resource: resource.spec.get("named_by") == "host",
    created_from="automation",
    removal_note=lambda _spec: "HQ forgets its scan results. The next scan records them again.",
    unobserved_reason="Each scan reports what it found.",
)

DEFINITIONS = (OPERATION, SOURCE)
