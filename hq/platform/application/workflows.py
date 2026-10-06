"""Domain-neutral resolution plans derived from claims and registered actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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

    identity = claim_identity(namespace, rule, subject, scope)
    return WorkflowPlan(
        id=f"resolve:{identity}",
        label="What to do",
        steps=tuple(steps),
        outcome=WorkflowOutcome(
            "claim_absent",
            identity,
            "",
        ),
    )


def serialize_workflow(plan: WorkflowPlan | None):
    """JSON-safe contract shared by API and MCP finding adapters."""

    return asdict(plan) if plan is not None else None


@dataclass(frozen=True)
class WorkflowLayout:
    """A claim as a card shows it: the remedies lead as the fix, and where to
    look and how to confirm are one line of links."""

    fix: tuple[ActionLink, ...] = ()
    impact: tuple[ActionLink, ...] = ()
    related: tuple[ActionLink, ...] = ()
    confirm: tuple[ActionLink, ...] = ()

    @property
    def links(self) -> bool:
        return bool(self.impact or self.related or self.confirm)


def workflow_layout(
    plan: WorkflowPlan | None,
    *,
    investigations: tuple[ActionLink, ...] = (),
    offers: tuple[ActionLink, ...] = (),
) -> WorkflowLayout:
    """A plan's remedies and confirmation, beside the claim's own links: what
    it affects and the pages it concerns, each named once."""

    phases = {step.phase: step.actions for step in plan.steps} if plan is not None else {}
    seen: set[str] = set()
    related = []
    for action in _dedupe(offers):
        if action.label not in seen:
            seen.add(action.label)
            related.append(action)
    return WorkflowLayout(
        fix=phases.get("act", ()),
        impact=_dedupe(investigations),
        related=tuple(related),
        confirm=phases.get("verify", ()),
    )
