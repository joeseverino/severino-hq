"""Open a playbook's form on inputs HQ holds: a finding's current, authorized
ones, or a run's own for Run again."""

from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import TypeAdapter, ValidationError

from hq.domains.control_plane.models import OperationRequest

from .finding_model import PLAYBOOK_INVESTIGATION, LookInto
lazy from . import automation_runs
lazy from .findings import estate_findings, finding_key, rule_for, rule_of

if TYPE_CHECKING:
    from .security import Principal


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
                return _investigation_fields(capability, hosts, finding.look_into)
    return None


def run_again(capability: str, run_id: str) -> dict[str, Any]:
    """What Run again fills the playbook's form with from the run ``run_id``
    of it: its machines, and its parameters only where it acted, so a
    preview's inputs never fill a run that acts. Empty for any other run."""

    # An id that is no UUID names no run.
    with suppress(DjangoValidationError):
        run = OperationRequest.objects.filter(pk=run_id, action=OperationRequest.Action.RUN).first()
        asked = (run.input or {}) if run is not None else {}
        if asked and automation_runs.capability_name(str(asked.get("operation", ""))) == capability:
            parameters = {} if automation_runs.CHECK in asked else asked.get("parameters") or {}
            return {
                "hosts": list(asked.get("hosts") or ()),
                **{f"parameters.{name}": value for name, value in parameters.items()},
            }
    return {}


def _investigation_fields(capability: str, hosts: list[str], look_into: LookInto) -> dict[str, Any]:
    initial: dict[str, Any] = {"hosts": hosts}
    manifest = next(
        (
            each
            for each in automation_runs.operation_registry().values()
            if automation_runs.capability_name(each.id) == capability
        ),
        None,
    )
    declared = manifest.input_type.model_fields if manifest else {}
    for name, value in look_into.inputs.items():
        if field := declared.get(name):
            with suppress(ValidationError):
                initial[f"parameters.{name}"] = TypeAdapter(field.rebuild_annotation()).validate_python(
                    value, strict=True
                )
    return initial
