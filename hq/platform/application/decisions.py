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

from .tone import Tone
lazy from .entity_links import web_url
lazy from .findings import rule_origin
lazy from .freshness import cadence, freshness
lazy from .moments import duration, moment
lazy from .routes import reverse
lazy from .ui import ended

NOTICE = "Notice"
# A step a person reads without scrolling: past either, the steps fold.
_SHORT_STEP = 120
_SHORT_STEPS = 2
# A next step said as a label is a link; said as a sentence it is advice.
_LABEL_LENGTH = 40


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


def _late(item: dict[str, Any]) -> dict[str, Any] | None:
    """The reading a row is drawn from, said on the row only once it is out
    of date for its kind, with how often that kind is read."""

    kind = item.get("reads", "")
    reading = freshness(kind, moment(item.get("observed_at", ""))) if kind else None
    if reading is None or not reading.stale:
        return None
    return {"reading": reading, "every": f"Read every {duration(cadence(kind).every).removeprefix('1 ')}"}


def _one_primary(actions: list[dict[str, Any]], named: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first recommended action leads, else the owner's named page; every
    other is a plain button, so a card never shows two of equal weight
    competing to be the fix."""

    lead = next((index for index, action in enumerate(actions) if action.get("recommended")), None)
    if lead is None and named:
        lead = actions.index(named[0])
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
    """Why an item is as urgent as it is, in its raiser's words (``Insight.why``)
    and what clears it (``Insight.clears``), then what raised it: the domain and
    the key it raised it under, and for a finding its rule and where that is
    declared. ``panel`` is the id its panel and the pill that opens it share,
    short since a page writes it five times for each item."""

    key = str(item.get("key", ""))
    _, finding, rest = key.partition(":finding:")
    rule = rule_origin(rest.split(":", 1)[0]) if finding else None
    why, clears = item.get("why", ""), item.get("clears", "")
    return {
        "why": f"{why} {ended(f'Clears once {clears}')}" if why and clears else why,
        "source": item.get("source", ""),
        "key": key,
        "panel": f"origin-{_digest(key)}",
        **(rule or {}),
    }


def item_anchor(key: str) -> str:
    """Where an item's card is on the queue page, stable while its key is."""

    return f"item-{_digest(key)}"


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _own_page(item: dict[str, Any], subject: dict[str, Any], anchor: str) -> str:
    """The page that shows an item's whole card: the thing it is about, the
    held request it describes (``Insight.held``), or the queue with its card
    in view."""

    if subject.get("url"):
        return subject["url"]
    if item.get("url") and item.get("held"):
        return item["url"]
    return f"{reverse('action_items')}#{anchor}"


def _buttons(item: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """An item's own actions and the plan's actions that stand beside them,
    each once, with the plan as a card shows it."""

    actions = [_plain(action) for action in item.get("actions", ())]
    parts = workflow_parts(item.get("workflow"))
    for action in _promoted(parts["steps"], offered=bool(actions)):
        if _identity(action) not in {_identity(known) for known in actions}:
            actions.append(action)
    return actions, parts


def _arranged(actions: list[dict[str, Any]], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A check again waits behind the owner's named page, so the two that
    # stand on the row are the fix and where it is done.
    return _one_primary(
        [action for action in actions if action.get("name") != "verify"]
        + links
        + [action for action in actions if action.get("name") == "verify"],
        links,
    )


def one_press(item: Any) -> bool:
    """Whether HQ does what ``item`` asks in one press: the button its row
    leads with runs an HQ command that fixes it (not a check again) and
    would run now. Reads its ``actions`` and ``workflow``, each as a
    dataclass or as written."""

    actions, _ = _buttons(item)
    lead = next((action for action in _arranged(actions, []) if action["recommended"]), None)
    return bool(lead and lead.get("capability") and lead.get("name") != "verify" and not lead.get("refusal"))


def decision(item: dict[str, Any]) -> dict[str, Any]:
    actions, parts = _buttons(item)
    workflow = item.get("workflow")
    identities = {_identity(action) for action in actions}
    steps = []
    for step in parts["steps"]:
        remaining = [action for action in step["actions"] if _identity(action) not in identities]
        if remaining or step["summary"]:
            steps.append({**step, "actions": remaining})
    subject = item.get("subject") or {}
    links, advice = _next_step(item, identities)
    anchor = item_anchor(str(item.get("key", "")))
    return {
        **item,
        "href": subject.get("url") or item.get("url") or "",
        "anchor": anchor,
        "page": _own_page(item, subject, anchor),
        "urgency": NOTICE if item.get("notice") else Tone(item["status"]).urgency,
        "actions": _arranged(actions, links),
        "late": _late(item),
        "facts": _facts(item.get("facts", ())),
        "origin": _origin(item),
        "advice": advice,
        "reason": parts["reason"],
        "steps": steps,
        "workflow": {**workflow, "steps": steps} if workflow and steps else None,
    }
