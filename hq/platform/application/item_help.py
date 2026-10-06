"""What an action item gives the operator besides the problem.

Every item HQ raises, a finding or an entry in the action queue, comes with
help, and never with a problem alone:

- a remedy: a capability HQ runs, offered through its owner's route;
- a command: the exact command or setting HQ derived, for the operator to run;
- a place: the page where the work is done, named by the item's own next step;
- an instruction: what to do, in words, where no command can be named;
- a reason: why none of those can be offered yet.

``Insight`` is the SDK's shape (``hq_sdk/contract.json``), so help travels in
fields it already has: a remedy as one of its ``actions``, a place as its
``action`` and ``url``, a command, an instruction or a reason as a step of its
``workflow``, told apart by the step's phase. A card shows each as what it is
(``application.decisions``): a reason is a sentence and never a step. The
contract test (``application/tests/test_item_help.py``) holds every host provider
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
PLACE = "place"
INSTRUCTION = "instruction"
REASON = "reason"
# Workflow step phases. "act" is the resolution plan's own phase for remedies.
RUN = "run"
DO = "do"
CANNOT = "cannot"
_NAMESPACE = "attention"


def _plan(key: str, steps: tuple[WorkflowStep, ...]) -> WorkflowPlan:
    identity = claim_identity(_NAMESPACE, key, "")
    return WorkflowPlan(
        id=f"help:{identity}",
        label="What to do",
        steps=steps,
        outcome=WorkflowOutcome("claim_absent", identity, ""),
    )


def run_step(label: str, command: str) -> WorkflowStep:
    """One exact command the operator runs; HQ never runs it."""

    return WorkflowStep(RUN, label, command, "operator")


def do_step(instruction: str) -> WorkflowStep:
    """What to do, in words, where no exact command can be named."""

    return WorkflowStep(DO, "", instruction, "operator")


def cannot_step(reason: str) -> WorkflowStep:
    """Why this one cannot be done from here. A card says the reason as a
    sentence and never shows this label; it names the step for API and MCP."""

    return WorkflowStep(CANNOT, "Why HQ cannot do this for you", reason, "blocked")


def commands(
    key: str, runs: Iterable[tuple[str, str]], *, then: str = "", reason: str = ""
) -> WorkflowPlan:
    """Commands to run, in order, as ``(label, command)`` pairs; ``then`` is a
    last step said in words, and ``reason`` is why they cannot be run from HQ,
    where that is worth saying."""

    steps = tuple(run_step(label, command) for label, command in runs)
    steps += (do_step(then),) if then else ()
    return _plan(key, steps + ((cannot_step(reason),) if reason else ()))


def instructions(key: str, *told: str, reason: str = "") -> WorkflowPlan:
    """What to do, in words, in order, where no exact command can be named."""

    steps = tuple(do_step(text) for text in told)
    return _plan(key, steps + ((cannot_step(reason),) if reason else ()))


def cannot_help(key: str, reason: str) -> WorkflowPlan:
    """The reason there is no remedy, command or instruction for this yet."""

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
    plan's remedies, then its commands; with neither, what to do in words and
    the reason it has neither."""

    steps = tuple(finding.workflow.steps) if finding.workflow is not None else ()
    runs = tuple(run_step(step.label, step.command) for step in finding.steps if step.command)
    if finding.remedies or runs:
        return _plan(key, steps + runs)
    told = tuple(do_step(step.label) for step in finding.steps if step.label.strip())
    reason = finding.no_help_reason
    return _plan(key, steps + told + ((cannot_step(reason),) if reason else ()))


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
        elif step.phase == DO and step.summary.strip():
            kinds.add(INSTRUCTION)
        elif step.phase == CANNOT and step.summary.strip():
            kinds.add(REASON)
    return next((kind for kind in (REMEDY, COMMAND, INSTRUCTION, REASON) if kind in kinds), "")


def item_help(item: Any) -> str:
    """Which help an action item carries, by the names above; blank for none.

    A remedy is an action that names a capability or posts to its owner's
    route; anything else on ``actions`` is only a link to look at. A place is
    the item's own next step with the page it is done on.
    """

    if any(action.capability or action.method == "POST" for action in getattr(item, "actions", ()) or ()):
        return REMEDY
    planned = _plan_help(getattr(item, "workflow", None))
    if planned in (REMEDY, COMMAND):
        return planned
    if str(getattr(item, "action", "") or "").strip() and getattr(item, "url", ""):
        return PLACE
    return planned
