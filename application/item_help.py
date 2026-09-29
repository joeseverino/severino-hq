"""What an action item gives the operator besides the problem.

Every item HQ raises, a finding or an entry in the action queue, comes with
one of three kinds of help, and never with prose that leaves the operator to
work it out:

- a remedy: a capability HQ runs, offered through its owner's route;
- a command: the exact command or setting HQ derived, for the operator to run;
- a reason: the specific reason HQ cannot do either yet.

``Insight`` is the SDK's shape (``hq_sdk/contract.json``), so help travels in
fields it already has: a remedy as one of its ``actions``, a command or a
reason as a step of its ``workflow``, told apart by the step's phase. The
contract test (``application/test_item_help.py``) holds every host provider
to it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .action_links import command_url
from .finding_model import Finding
from .workflow_contracts import ActionLink, WorkflowOutcome, WorkflowPlan, WorkflowStep
from .workflows import claim_identity

REMEDY = "remedy"
COMMAND = "command"
REASON = "reason"
# Workflow step phases. "act" is the resolution plan's own phase for remedies.
RUN = "run"
CANNOT = "cannot"
_NAMESPACE = "attention"


def _plan(key: str, steps: tuple[WorkflowStep, ...]) -> WorkflowPlan:
    identity = claim_identity(_NAMESPACE, key, "")
    return WorkflowPlan(
        id=f"help:{identity}",
        label="Steps to resolve",
        steps=steps,
        outcome=WorkflowOutcome("claim_absent", identity, "Done when HQ no longer raises this."),
    )


def run_step(label: str, command: str) -> WorkflowStep:
    """One exact command the operator runs; HQ never runs it."""

    return WorkflowStep(RUN, label, command, "operator")


def cannot_step(reason: str) -> WorkflowStep:
    return WorkflowStep(CANNOT, "Why HQ cannot do this for you", reason, "blocked")


def commands(key: str, runs: Iterable[tuple[str, str]], *, reason: str = "") -> WorkflowPlan:
    """Commands to run, in order, as ``(label, command)`` pairs; with a reason
    too when HQ can only name them and not run them."""

    steps = tuple(run_step(label, command) for label, command in runs)
    return _plan(key, steps + ((cannot_step(reason),) if reason else ()))


def cannot_help(key: str, reason: str) -> WorkflowPlan:
    """HQ's specific reason it has no remedy and no command for this yet."""

    return _plan(key, (cannot_step(reason),))


def remedy_link(capability: str, label: str, target: str = "", *, url: str = "") -> ActionLink | None:
    """A capability HQ runs, through its command page or the owner's own page.

    Links, never executes: the destination authorizes and confirms. None when
    the command route is not mounted, so an item never offers a dead link.
    """

    if not url:
        url = command_url(capability, target)
        if not url:
            return None
    return ActionLink(REMEDY, label, "remote_write", url, capability=capability, target=target, recommended=True)


def finding_plan(finding: Finding, key: str) -> WorkflowPlan:
    """A finding's own help, as an action item carries it: its resolution
    plan's remedies, then its commands, else the reason it has neither."""

    steps = tuple(finding.workflow.steps) if finding.workflow is not None else ()
    runs = tuple(run_step(step.label, step.command) for step in finding.steps if step.command)
    reason = finding.no_help_reason if not (finding.remedies or runs) else ""
    return _plan(key, steps + runs + ((cannot_step(reason),) if reason else ()))


def finding_help(finding: Finding) -> str:
    """Which help a finding carries, or blank when it carries none."""

    if finding.remedies:
        return REMEDY
    if any(step.command for step in finding.steps):
        return COMMAND
    return REASON if finding.no_help_reason.strip() else ""


def _plan_help(plan: WorkflowPlan | None) -> str:
    kinds = set()
    for step in plan.steps if plan is not None else ():
        if step.phase == "act" and any(action.recommended for action in step.actions):
            kinds.add(REMEDY)
        elif step.phase == RUN and step.summary.strip():
            kinds.add(COMMAND)
        elif step.phase == CANNOT and step.summary.strip():
            kinds.add(REASON)
    return next((kind for kind in (REMEDY, COMMAND, REASON) if kind in kinds), "")


def item_help(item: Any) -> str:
    """Which help an action item carries: remedy, command or reason; blank for none.

    A remedy is an action that names a capability or posts to its owner's
    route; anything else on ``actions`` is only a link to look at.
    """

    if any(action.capability or action.method == "POST" for action in getattr(item, "actions", ()) or ()):
        return REMEDY
    return _plan_help(getattr(item, "workflow", None))
