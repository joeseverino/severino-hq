"""Open a finding's playbook on its current, authorized inputs."""

from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from pydantic import TypeAdapter, ValidationError

from .finding_model import PLAYBOOK_INVESTIGATION
lazy from . import automation_runs
lazy from .findings import estate_findings, finding_key, rule_for, rule_of

if TYPE_CHECKING:
    from .security import Principal

_SERVICE_UNIT = "service_unit"


def finding_run(capability: str, key: str, principal: Principal) -> dict[str, Any] | None:
    """The values a finding's investigation fills its playbook's form with,
    only while this reader's finding offers that command."""

    if not automation_runs.runs_procedure(capability) or rule_for(rule := rule_of(key)) is None:
        return None
    for finding in estate_findings(principal=principal, rule=rule):
        if finding_key(finding) != key:
            continue
        for offer in finding.offers:
            if offer.name == PLAYBOOK_INVESTIGATION and offer.capability == capability and not offer.refusal:
                hosts = parse_qs(urlsplit(offer.url).query)["hosts"]
                return _investigation_fields(capability, hosts, finding.service_unit)
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
