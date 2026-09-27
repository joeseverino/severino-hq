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
                "Check the impact",
                "See what depends on it before changing anything.",
                "available",
                inspect_actions,
            )
        )
    if act_actions:
        steps.append(
            WorkflowStep(
                "act",
                "Fix it",
                "Run one of these actions.",
                "recommended" if remedies else "available",
                act_actions,
            )
        )
    if verification is not None:
        steps.append(
            WorkflowStep(
                "verify",
                "Confirm the fix",
                f"{verification.label}. It is resolved when this finding is gone.",
                "after_action" if remedies else "available",
                (verification,),
            )
        )

    identity = claim_identity(namespace, rule, subject, scope)
    return WorkflowPlan(
        id=f"resolve:{identity}",
        label="Steps to resolve",
        steps=tuple(steps),
        outcome=WorkflowOutcome(
            "claim_absent",
            identity,
            "Done when a fresh check no longer finds this.",
        ),
    )


def serialize_workflow(plan: WorkflowPlan | None):
    """JSON-safe contract shared by API and MCP finding adapters."""

    return asdict(plan) if plan is not None else None


@dataclass(frozen=True)
class WorkflowLayout:
    """A plan as a card shows it: the gated remedies lead as the fix, and the
    rest is one line of links."""

    fix: tuple[ActionLink, ...] = ()
    impact: tuple[ActionLink, ...] = ()
    related: tuple[ActionLink, ...] = ()
    confirm: tuple[ActionLink, ...] = ()

    @property
    def links(self) -> bool:
        return bool(self.impact or self.related or self.confirm)


def workflow_layout(plan: WorkflowPlan | None) -> WorkflowLayout:
    """Split a plan's actions by what they are: the recommended remedies, what
    to check first, other places to look, and how to confirm."""

    if plan is None:
        return WorkflowLayout()
    phases = {step.phase: step.actions for step in plan.steps}
    act = phases.get("act", ())
    return WorkflowLayout(
        fix=tuple(action for action in act if action.recommended),
        impact=phases.get("understand", ()),
        related=tuple(action for action in act if not action.recommended),
        confirm=phases.get("verify", ()),
    )
