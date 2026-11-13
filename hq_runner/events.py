"""A run's task outcomes as its report carries them: one per task and machine,
struck of secrets and held to the bridge's ``RunEvent`` bounds.

An event is built from four fields of what Ansible said (the host, the task's
name, its outcome and its own one-line ``msg``), never from its whole result,
its stdout or its arguments, so what a module returned beside its message
never leaves the worker. A task marked ``no_log`` says only that it was hidden.
HQ strikes the credential shapes it knows again at the bridge's door; this is
the worker's own strike, of what it alone knows to be secret.
"""

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The bridge contract's RunEvent bounds, and its report's.
HOST_LIMIT = 253
TASK_LIMIT = 200
MESSAGE_LIMIT = 500
EVENT_LIMIT = 200

REDACTED = "[redacted]"
HIDDEN = "The task's output is hidden (no_log)."

# Ansible's host events, by the outcome each reports.
_OUTCOMES = {
    "runner_on_ok": "ok",
    "runner_on_failed": "failed",
    "runner_on_skipped": "skipped",
    "runner_on_unreachable": "unreachable",
}

# Shapes a secret has whoever wrote it: a private key, from its header to its
# footer or the end of a text cut short, and a value given under a name that
# says it is one (``password=...``, ``"token": "..."``).
# The names, not a secret: S105 reads any string assigned to such a name as one.
_SECRET_NAMES = r"(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|credential)"  # noqa: S105
_SHAPES = re.compile(
    r"(?:-----BEGIN [A-Z0-9 ]*)?PRIVATE KEY(?: BLOCK)?-----(?s:.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|.*)"
    rf"|(?i:(?P<name>\b\w*{_SECRET_NAMES}\w*['\"]?\s*[:=]\s*['\"]?))[^\s'\",}}]+"
)
_NAMED = re.compile(_SECRET_NAMES, re.IGNORECASE)
# A learned value shorter than this is not struck: a short word would strike
# ordinary text.
_SHORTEST_SECRET = 6


@dataclass(frozen=True, slots=True)
class Event:
    host: str
    task: str
    outcome: str
    message: str

    def as_report(self) -> dict[str, str]:
        return {"host": self.host, "task": self.task, "outcome": self.outcome, "message": self.message}


def learned(parameters: Mapping[str, Any]) -> frozenset[str]:
    """The values of a run's parameters given under a name that says they are
    secret, at any depth, which the worker strikes wherever they appear."""

    found: set[str] = set()

    def walk(node: Any, *, named: bool) -> None:
        if isinstance(node, str) and named and len(node) >= _SHORTEST_SECRET:
            found.add(node)
        elif isinstance(node, Mapping):
            for key, child in node.items():
                walk(child, named=named or bool(_NAMED.search(str(key))))
        elif isinstance(node, list):
            for child in node:
                walk(child, named=named)

    walk(parameters, named=False)
    return frozenset(found)


def strike(text: str, secrets: Collection[str] = ()) -> str:
    """``text`` with every secret shape and every one of ``secrets`` struck."""

    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return _SHAPES.sub(lambda found: (found.group("name") or "") + REDACTED, text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def from_ansible(event: Mapping[str, Any], secrets: Collection[str] = ()) -> Event | None:
    """The outcome one ansible-runner event reports, or None for an event that
    is not a task's outcome on a machine."""

    outcome = _OUTCOMES.get(str(event.get("event", "")))
    data = event.get("event_data")
    if outcome is None or not isinstance(data, Mapping):
        return None
    result = data.get("res")
    result = result if isinstance(result, Mapping) else {}
    if outcome == "ok" and result.get("changed") is True:
        outcome = "changed"
    # "censored" is Ansible's own stand-in for the result of a task marked no_log.
    message = HIDDEN if "censored" in result else strike(_one_line(result.get("msg", "")), secrets)
    host = strike(_one_line(data.get("host", "")), secrets)
    task = strike(_one_line(data.get("task", "")), secrets)
    return Event(_clip(host, HOST_LIMIT), _clip(task, TASK_LIMIT), outcome, _clip(message, MESSAGE_LIMIT))


# The outcomes a report keeps first when a run has more than it can carry:
# what changed a machine or went wrong.
_KEPT_FIRST = frozenset({"changed", "failed", "unreachable"})


def bounded(events: Iterable[Event]) -> tuple[list[Event], int]:
    """At most ``EVENT_LIMIT`` of ``events``, in order, keeping what changed or
    failed ahead of what was fine or skipped, and how many were left out."""

    every = list(events)
    if len(every) <= EVENT_LIMIT:
        return every, 0
    ranked = sorted(range(len(every)), key=lambda index: (every[index].outcome not in _KEPT_FIRST, index))
    kept = sorted(ranked[:EVENT_LIMIT])
    return [every[index] for index in kept], len(every) - EVENT_LIMIT
