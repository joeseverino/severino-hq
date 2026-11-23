"""Review a group of eligible image updates and queue it as one guarded run."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from hq.platform.core.digests import digest

from . import automation_runs, container_findings
from .action_links import command_url
from .approval_review import ChangePreview
from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
from .automation_manifest import DottedName, OperationManifest
from .capabilities import Consented, consent
from .command_fields import Describe
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .payload_words import ChangeRow
from .record_errors import PolicyError
from .resource_operations import RunAsked
from .security import Capability, Principal, internal_principal
lazy from .findings import estate_findings

NAME = "infrastructure.compose_batch.run"
REASON = "Deploy the reviewed image updates."
TITLE = "Review image updates"
DEPLOY = "Deploy images"
MAX_UPDATES = 10


@dataclass(frozen=True, slots=True)
class Batch:
    """The exact current image pins, each paired with its own machine."""

    manifest: OperationManifest
    updates: tuple[automation_runs.ComposeDeployment, ...]

    @property
    def review(self) -> str:
        return digest((automation_runs.pinned(self.manifest), self.steps))

    @property
    def hosts(self) -> list[str]:
        return sorted({update.machine for update in self.updates})

    @property
    def steps(self) -> list[dict]:
        return [{"machine": update.machine, "parameters": update.parameters} for update in self.updates]

    @property
    def query(self) -> dict[str, str]:
        return {"operation": self.manifest.id, "review": self.review}

    @property
    def url(self) -> str:
        return command_url(NAME, **self.query)


def available(principal: Principal) -> tuple[Batch, ...]:
    """Group compatible updates under the adopted procedure that offers them."""

    if not principal.permits(Capability.READ, Capability.MANAGE_INFRASTRUCTURE):
        return ()
    registry = automation_runs.operation_registry()
    groups: dict[str, dict[tuple[str, str, str], automation_runs.ComposeDeployment]] = defaultdict(dict)
    for finding in estate_findings(principal=principal):
        if finding.rule not in (container_findings.ADVISORY, container_findings.UPDATE):
            continue
        for update in finding.reviewed_updates:
            manifest = registry.get(update.operation)
            if manifest is None or manifest.revision != update.revision:
                continue
            address = (update.machine, update.pin.project, update.pin.service)
            groups[update.operation].setdefault(address, update)
    return tuple(
        Batch(registry[operation], tuple(updates[key] for key in sorted(updates)[:MAX_UPDATES]))
        for operation, updates in sorted(groups.items())
        if len(updates) > 1
    )


def reviewed(operation: str, review: str, principal: Principal) -> Batch | None:
    return next(
        (batch for batch in available(principal) if batch.manifest.id == operation and batch.review == review),
        None,
    )


@dataclass(frozen=True, slots=True)
class BatchCommand:
    operation: Annotated[DottedName, Describe("The adopted playbook named by the review.")]
    review: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$"), Describe("The review fingerprint shown by HQ.")]
    reason: automation_runs.Reason = REASON


def _bind(command: BatchCommand, principal: Principal) -> Batch:
    batch = reviewed(command.operation, command.review, principal)
    if batch is None:
        raise PolicyError("The available image updates changed. Open Needs you and review the current updates.")
    return batch


def _subject(command: BatchCommand) -> ApprovalSubject | None:
    batch = reviewed(command.operation, command.review, internal_principal("approval-baseline"))
    if batch is None:
        return None
    return ApprovalSubject(
        "automation.operation",
        automation_runs.resource_key(command.operation),
        {
            "spec": automation_runs.pinned(batch.manifest),
            "hosts": batch.hosts,
            "updates": batch.steps,
        },
    )


def _preview(held) -> ChangePreview:
    rows = [
        ChangeRow(
            label=f"{item['machine']} / {item['parameters']['compose_project_name']} / {item['parameters']['compose_project_upgrade_service']}",
            before="",
            after=item["parameters"]["compose_project_upgrade_image"],
            change="added",
        )
        for item in held.baseline.get("updates", ())
    ]
    if revision := held.baseline.get("spec", {}).get("revision"):
        rows.append(ChangeRow(label="Adopted commit", before="", after=revision[:12], change="added"))
    rows.append(ChangeRow(label="On failure", before="", after="Stop before deploying the next image", change="added"))
    return ChangePreview(label="Images to deploy in order", rows=tuple(rows), lines=())


def _request(command: BatchCommand, *, consented: Consented) -> RunAsked:
    batch = _bind(command, consent(consented).principal)
    hosts = automation_runs.resolved_hosts(batch.manifest, tuple(batch.hosts))
    automation_runs.reached(batch.manifest, hosts)
    if list(hosts) != batch.hosts:
        raise PolicyError("The machines in this review have changed. Open Needs you and review the current updates.")
    return automation_runs.queue_run(
        batch.manifest,
        {
            "operation": batch.manifest.id,
            "revision": batch.manifest.revision,
            "hosts": batch.hosts,
            "steps": batch.steps,
            "correlation": "",
        },
        reason=command.reason,
        consented=consented,
    )


def capability() -> CapabilitySpec:
    return CapabilitySpec(
        NAME,
        "Deploy the current reviewed Compose image updates as one run, stopping at the first failure.",
        Effect.INFRASTRUCTURE_CHANGE,
        Capability.MANAGE_INFRASTRUCTURE,
        BatchCommand,
        waits_for_a_person(every_call)(measured_by(_subject, shown=_preview)(_request)),
        label=TITLE,
        wants="deploy the reviewed image updates",
        reached_from="action_items",
    )
