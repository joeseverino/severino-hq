"""Domain-neutral resolution plans derived from claims and registered actions."""

from dataclasses import asdict
from hashlib import sha256
from typing import Annotated, Any

from .json_schemas import Written
from .workflow_contracts import ActionLink, WorkflowOutcome, WorkflowPlan, WorkflowStep

# A plan and an action as an answer carries them.
WrittenWorkflow = Annotated[dict[str, Any], Written(WorkflowPlan)]
WrittenAction = Annotated[dict[str, Any], Written(ActionLink)]


def claim_identity(namespace: str, key: str) -> str:
    """Stable identity for any domain claim across repeated derivations: the
    hash of the one key its source knows it by, so two claims a source tells
    apart never share an id."""

    digest = sha256(f"{namespace}\0{key}".encode()).hexdigest()[:16]
    return f"claim:{digest}"


def _dedupe(actions: tuple[ActionLink, ...]) -> tuple[ActionLink, ...]:
    seen: set[tuple[str, str]] = set()
    kept = []
    for action in actions:
        identity = (action.method, action.url)
        if not action.url or identity in seen:
            continue
        seen.add(identity)
        kept.append(action)
    return tuple(kept)


def claim_resolution_plan(
    *,
    identity: str,
    remedies: tuple[ActionLink, ...],
    verification: ActionLink | None,
) -> WorkflowPlan | None:
    """The steps that resolve a claim: a remedy, then the check that confirms
    it. Where to look is the claim's own links, not a step. A fix made by hand
    still has its check."""

    act_actions = _dedupe(remedies)
    if not act_actions and verification is None:
        return None
    steps = [WorkflowStep("act", "Fix it", "", "recommended", act_actions)] if act_actions else []
    if verification is not None:
        steps.append(
            WorkflowStep(
                "verify",
                "Check that it worked" if act_actions else "Check again",
                "",
                "after_action" if act_actions else "available",
                (verification,),
            )
        )

    return WorkflowPlan(
        id=f"resolve:{identity}",
        label="Next steps",
        steps=tuple(steps),
        outcome=WorkflowOutcome(
            "claim_absent",
            identity,
            "",
        ),
    )


def serialize_workflow(plan: WorkflowPlan | None) -> WrittenWorkflow | None:
    """JSON-safe contract shared by API and MCP finding adapters."""

    return asdict(plan) if plan is not None else None
