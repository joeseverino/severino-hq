"""Open a finding's playbook on its current, authorized inputs."""

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from pydantic import TypeAdapter, ValidationError

from .action_links import command_action
from .finding_model import PLAYBOOK_INVESTIGATION
lazy from . import automation_runs, container_findings
lazy from .findings import estate_findings, finding_key, rule_for, rule_of

if TYPE_CHECKING:
    from .automation_runs import ComposeDeployment
    from .security import Principal
    from .upgrades import ComposeTarget
    from .workflow_contracts import ActionLink

_SERVICE_UNIT = "service_unit"


@dataclass(frozen=True, slots=True)
class FindingRun:
    """The values the current finding can supply to its playbook's form."""

    initial: dict[str, Any]
    deployment: ComposeDeployment | None = None

    @property
    def review(self) -> str:
        return self.deployment.review if self.deployment else ""


def finding_run(capability: str, key: str, principal: Principal, *, review: str = "") -> FindingRun | None:
    """Fill the run only while this reader's finding offers that command."""

    if not automation_runs.runs_procedure(capability) or rule_for(rule := rule_of(key)) is None:
        return None
    for finding in estate_findings(principal=principal, rule=rule):
        if finding_key(finding) != key:
            continue
        for deployment in finding.reviewed_updates:
            current = automation_runs.operation_registry().get(deployment.operation)
            if (
                deployment.capability == capability
                and deployment.review == review
                and current is not None
                and deployment.revision == current.revision
            ):
                return FindingRun(deployment.initial, deployment)
        for offer in finding.offers:
            if offer.name == PLAYBOOK_INVESTIGATION and offer.capability == capability and not offer.refusal:
                hosts = parse_qs(urlsplit(offer.url).query)["hosts"]
                return FindingRun(_investigation_fields(capability, hosts, finding.service_unit))
    return None


def container_deployment(target: ComposeTarget, principal: Principal) -> ActionLink | None:
    """The upgrade plan and its finding offer the same deployment."""

    for rule in (container_findings.UPDATE, container_findings.ADVISORY):
        for finding in estate_findings(principal=principal, rule=rule):
            if target not in finding.update_targets:
                continue
            for deployment in finding.reviewed_updates:
                if (deployment.machine, deployment.pin.project, deployment.pin.service) == (
                    target.machine,
                    target.project,
                    target.service,
                ):
                    return command_action(
                        "remedy",
                        deployment.capability,
                        deployment.url,
                        intent=automation_runs.REVIEW_IMAGE_UPDATE,
                        recommended=True,
                    )
    return None


def _investigation_fields(capability: str, hosts: list[str], service: str) -> dict[str, Any]:
    initial: dict[str, Any] = {"hosts": hosts}
    if not service:
        return initial
    manifest = next(
        (
            each
            for each in automation_runs.operation_registry().values()
            if automation_runs.capability_name(each.id) == capability
        ),
        None,
    )
    if manifest and (field := manifest.input_type.model_fields.get(_SERVICE_UNIT)):
        with suppress(ValidationError):
            initial[f"parameters.{_SERVICE_UNIT}"] = TypeAdapter(field.rebuild_annotation()).validate_python(
                service, strict=True
            )
    return initial
