"""The face of a queue card, from what its owner emitted. No reads, no new decisions.

A card says what is wrong, what it means, since when, and what to do. What to
do is three different things and each is shown as what it is:

- a button: an action its owner offers, or the lone check that reads again;
- a step: a command to run or an instruction with its own text, numbered only
  when there is more than one;
- a sentence: the reason this one cannot be done from here.

Each action renders once; the transport keeps the owner's complete workflow.
"""

from dataclasses import asdict, is_dataclass
from typing import Any

# How urgent, in the words a card's pill says.
URGENCY = {"serious": "Urgent", "attention": "Needs attention", "neutral": "FYI", "good": "Fine"}
NOTICE = "Notice"
# A step a person reads without scrolling: past either, the steps fold.
_SHORT_STEP = 120
_SHORT_STEPS = 2
# A next step said as a label is a link; said as a sentence it is advice.
_LABEL_LENGTH = 40


def urgency(status: str) -> str:
    return URGENCY.get(status, URGENCY["attention"])


def _plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def _identity(action: dict[str, Any]) -> tuple[str, str]:
    return (action["method"], action["url"])


def _promoted(steps: list[dict[str, Any]], offered: bool) -> list[dict[str, Any]]:
    """The actions that stand on the card as buttons.

    The first step that can be taken now leads, unless the owner already put
    its own actions on the item. A check that says nothing but "read again" is
    always a button.
    """

    first = (
        []
        if offered
        else next(
            (
                list(step["actions"])
                for step in steps
                if step["state"] in {"recommended", "available"} and step["actions"]
            ),
            [],
        )
    )
    checks = [
        action
        for step in steps
        if step["phase"] == "verify" and step["state"] != "blocked" and not step["summary"]
        for action in step["actions"]
    ]
    return first + checks


def _fold(steps: list[dict[str, Any]]) -> bool:
    return len(steps) > _SHORT_STEPS or any(len(step["summary"]) > _SHORT_STEP for step in steps)


def workflow_parts(plan: Any) -> dict[str, Any]:
    """A plan as a card shows it: its real steps, and the reason apart from them."""

    plan = _plain(plan)
    steps = list(plan["steps"]) if plan else []
    real = [step for step in steps if step["phase"] != "cannot"]
    return {
        "steps": real,
        "reason": " ".join(
            step["summary"].strip()
            for step in steps
            if step["phase"] == "cannot" and step["summary"].strip()
        ),
        "fold": _fold(real),
    }


def _next_step(item: dict[str, Any], taken: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], str]:
    """The owner's named next step: a link to where it is done, or a sentence."""

    said = str(item.get("action") or "").strip()
    url = item.get("url") or ""
    if not said:
        return [], ""
    if not url or len(said) > _LABEL_LENGTH or said.endswith("."):
        return [], said
    if ("GET", url) in taken:
        return [], ""
    link = {
        "name": "open", "label": said, "effect": "read", "url": url, "method": "GET",
        "capability": "", "target": "", "reason": "", "recommended": False,
    }
    return [link], ""


def decision(item: dict[str, Any]) -> dict[str, Any]:
    actions = list(item.get("actions", ()))
    workflow = item.get("workflow")
    parts = workflow_parts(workflow)
    for action in _promoted(parts["steps"], bool(actions)):
        if _identity(action) not in {_identity(known) for known in actions}:
            actions.append(action)
    identities = {_identity(action) for action in actions}
    steps = []
    for step in parts["steps"]:
        remaining = [action for action in step["actions"] if _identity(action) not in identities]
        if remaining or step["summary"]:
            steps.append({**step, "actions": remaining})
    subject = item.get("subject") or {}
    links, advice = _next_step(item, identities)
    return {
        **item,
        "href": subject.get("url") or item.get("url") or "",
        "urgency": NOTICE if item.get("notice") else urgency(item.get("status", "")),
        "actions": actions + links,
        "advice": advice,
        "reason": parts["reason"],
        "steps": steps,
        "steps_fold": _fold(steps),
        "workflow": {**workflow, "steps": steps} if workflow and steps else None,
    }
