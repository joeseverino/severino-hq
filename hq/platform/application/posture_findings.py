"""Under Advanced HQ Posture, each check of HQ's standard read and found failing.

A check not read yet is no finding: the posture panel shows it waiting, with
what would read it.
"""

from .finding_model import THEN_CHECK_AGAIN, Finding, FindingEstate, FindingRule, OperatorStep
from .posture import STANDARD, PostureCheck, advanced
from .ui import ended

_BY_ID = {check.id: check for check in STANDARD}
_SCOPE = "posture."


def posture_check(scope: str) -> PostureCheck | None:
    """The check a posture finding's ``scope`` names, else None."""

    return _BY_ID.get(scope.removeprefix(_SCOPE)) if scope.startswith(_SCOPE) else None


def _unmet(estate: FindingEstate) -> tuple[Finding, ...]:
    if not advanced():
        return ()
    found = []
    for check_id, evidence, read_capabilities in estate.topology.unmet_standard:
        check = _BY_ID[check_id]
        found.append(
            Finding(
                rule="posture-unmet",
                subject="",
                scope=f"{_SCOPE}{check.id}",
                read_capabilities=read_capabilities,
                title=check.unmet,
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
