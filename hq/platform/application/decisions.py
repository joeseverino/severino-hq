"""The face of a queue card, from what its owner emitted. No reads, no new decisions.

A card says what is wrong, what it means, since when, and what to do, shown as
what it is: a button for an action its owner offers (or the lone check that
reads again), a step for a command or instruction (numbered only when there
are several), a sentence for why it cannot be done from here. Each action
renders once; the transport keeps the owner's complete workflow.
"""

import hashlib
from dataclasses import asdict, is_dataclass
from typing import Any

lazy from .approvals import APPROVE, REJECT
lazy from .entity_links import web_url
lazy from .findings import rule_origin
lazy from .routes import reverse

# How urgent, in the words a card's pill says.
URGENCY = {"serious": "Urgent", "attention": "Needs attention", "neutral": "Note", "good": "Fine"}
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


def _promoted(steps: list[dict[str, Any]], *, offered: bool) -> list[dict[str, Any]]:
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
            step["summary"].strip() for step in steps if step["phase"] == "cannot" and step["summary"].strip()
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
        "name": "open",
        "label": said,
        "effect": "read",
        "url": url,
        "method": "GET",
        "capability": "",
        "target": "",
        "reason": "",
        "recommended": False,
    }
    return [link], ""


def _decided_here(actions: list[dict[str, Any]]) -> bool:
    """Whether the card's own buttons decide what its meaning describes, as an
    approval's do: its meaning is then shown whole, never cut to a line, so
    nothing is approved on half of what it changes."""

    return any(_plain(action).get("name") in (APPROVE, REJECT) for action in actions)


def _one_primary(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first recommended action leads; every other is a plain button, so a
    card never shows two of equal weight competing to be the fix."""

    lead = next((index for index, action in enumerate(actions) if action.get("recommended")), None)
    return [{**action, "recommended": index == lead} for index, action in enumerate(actions)]


def _facts(pairs: Any) -> list[dict[str, Any]]:
    """Facts by label, in the order first given: each label once, with every
    value it was given, and a value that is a web address as a link."""

    grouped: dict[str, list[dict[str, str]]] = {}
    for label, value in pairs:
        text = str(value)
        grouped.setdefault(str(label), []).append({"text": text, "url": web_url(text)})
    return [{"label": label, "values": values} for label, values in grouped.items()]


def _origin(item: dict[str, Any]) -> dict[str, Any]:
    """What raised an item: a finding's rule and where it is declared, or the
    domain that raised anything else and the key it raised it under."""

    key = str(item.get("key", ""))
    _, finding, rest = key.partition(":finding:")
    rule = rule_origin(rest.split(":", 1)[0]) if finding else None
    return {"source": item.get("source", ""), "key": key, **(rule or {})}


def _anchor(key: str) -> str:
    """Where an item's card is on the queue page, stable while its key is."""

    return "item-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _own_page(subject: dict[str, Any], anchor: str) -> str:
    """The page that shows an item's whole card: the thing it is about, or the
    queue with its card in view."""

    return subject.get("url") or f"{reverse('action_items')}#{anchor}"


def decision(item: dict[str, Any]) -> dict[str, Any]:
    actions = list(item.get("actions", ()))
    workflow = item.get("workflow")
    parts = workflow_parts(workflow)
    for action in _promoted(parts["steps"], offered=bool(actions)):
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
    anchor = _anchor(str(item.get("key", "")))
    return {
        **item,
        "href": subject.get("url") or item.get("url") or "",
        "anchor": anchor,
        "page": _own_page(subject, anchor),
        "urgency": NOTICE if item.get("notice") else urgency(item.get("status", "")),
        "actions": _one_primary(actions + links),
        "whole": _decided_here(actions),
        "facts": _facts(item.get("facts", ())),
        "origin": _origin(item),
        "advice": advice,
        "reason": parts["reason"],
        "steps": steps,
        "steps_fold": _fold(steps),
        "workflow": {**workflow, "steps": steps} if workflow and steps else None,
    }
