"""Domain-neutral resolution plans derived from claims and registered actions."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256

from .workflow_contracts import ActionLink, WorkflowOutcome, WorkflowPlan, WorkflowStep


def claim_identity(namespace: str, rule: str, subject: str, scope: str = "") -> str:
    """Stable identity for any domain claim across repeated derivations."""

    digest = sha256(
        f"{namespace}\0{rule}\0{subject}\0{scope}".encode()
    ).hexdigest()[:16]
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
    namespace: str,
    rule: str,
    subject: str,
    scope: str,
    investigations: tuple[ActionLink, ...],
    offers: tuple[ActionLink, ...],
    remedies: tuple[ActionLink, ...],
    verification: ActionLink | None,
) -> WorkflowPlan | None:
    """Derive an honest resolution loop without inventing an execution path."""

    inspect_actions = _dedupe(investigations)
    act_actions = _dedupe((*remedies, *offers))
    if not inspect_actions and not act_actions:
        return None

    steps = []
    if inspect_actions:
        steps.append(
            WorkflowStep(
                "understand",
                "Understand the impact",
                "Inspect the supporting relationships before changing anything.",
                "available",
                inspect_actions,
            )
        )
    if act_actions:
        steps.append(
            WorkflowStep(
                "act",
                "Act through HQ",
                "Use an authorized workflow already owned by the affected subject.",
                "recommended" if remedies else "available",
                act_actions,
            )
        )
    if verification is not None:
        steps.append(
            WorkflowStep(
                "verify",
                "Verify from fresh facts",
                "Re-derive the claim; resolution means HQ can no longer prove it.",
                "after_action" if remedies else "available",
                (verification,),
            )
        )

    identity = claim_identity(namespace, rule, subject, scope)
    return WorkflowPlan(
        id=f"resolve:{identity}",
        label="Resolution workflow",
        steps=tuple(steps),
        outcome=WorkflowOutcome(
            "claim_absent",
            identity,
            "Complete when this claim is absent from a fresh derivation.",
        ),
    )


def serialize_workflow(plan: WorkflowPlan | None):
    """JSON-safe contract shared by API and MCP finding adapters."""

    return asdict(plan) if plan is not None else None
