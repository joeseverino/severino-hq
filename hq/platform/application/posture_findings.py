"""Under Advanced HQ Posture, each unmet check of HQ's standard."""

from .finding_model import THEN_CHECK_AGAIN, Finding, FindingEstate, FindingRule, OperatorStep
from .posture import STANDARD, advanced
from .ui import ended

_BY_ID = {check.id: check for check in STANDARD}


def _unmet(estate: FindingEstate) -> tuple[Finding, ...]:
    if not advanced():
        return ()
    found = []
    for check_id, evidence, read in estate.topology.unmet_standard:
        check = _BY_ID[check_id]
        found.append(
            Finding(
                rule="posture-unmet",
                subject="",
                scope=f"posture.{check.id}",
                title=f"HQ has not read whether {check.label[0].lower()}{check.label[1:]}" if not read else check.unmet,
                severity="attention",
                explanation=ended(f"Stops {check.stops}"),
                evidence=((check.label, evidence),),
                steps=(OperatorStep(label=check.step, notes=(THEN_CHECK_AGAIN,)),),
            )
        )
    return tuple(found)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "posture-unmet",
        "A check of HQ's standard is not met",
        "attention",
        _unmet,
        operator_action="Meet the check, or turn Advanced HQ Posture off.",
        no_help_reason="HQ does not change these settings.",
    ),
)
