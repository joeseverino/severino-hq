"""A run's task outcomes as its report carries them: one per task and machine,
struck of secrets and held to the bridge's ``RunEvent`` bounds.

An event is built from four fields of what Ansible said (the host, the task's
name, its outcome and its own one-line ``msg``), never from its whole result,
its stdout or its arguments, so what a module returned beside its message
never leaves the worker. A task marked ``no_log`` says only that it was hidden.
The one line of stdout read is the error that stops a run before any task
ends (``Stopped``), struck as a task's message is. HQ strikes the credential
shapes it knows again at the bridge's door; this is the worker's own strike,
of what it alone knows to be secret.
"""

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
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

# The action of Ansible's own check of a role's arguments, which it runs
# before every role that declares an argument spec. It is no task of the
# playbook, so a run reports it only when it fails, which is why nothing of
# the role ran.
ARGUMENT_CHECK = "ansible.builtin.validate_argument_spec"

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
    is not a playbook task's outcome on a machine."""

    outcome = _OUTCOMES.get(str(event.get("event", "")))
    data = event.get("event_data")
    if outcome is None or not isinstance(data, Mapping):
        return None
    if data.get("resolved_action") == ARGUMENT_CHECK and outcome != "failed":
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


# How ansible-core 2.22 begins a line it prints as an error
# (``ansible/utils/display.py``, ``Display.error``), and the line after it
# that says where the error is, as ``<path>:<line>:<column>``.
ERROR = "[ERROR]: "
ORIGIN = "Origin: "
# A terminal's color code, which Ansible wraps an error line in.
_COLOR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _printed(event: Mapping[str, Any]) -> str | None:
    """The line Ansible printed outside any task, as ansible-runner 2.4.3
    delivers one: a ``verbose`` event whose ``stdout`` is that line
    (``ansible_runner/utils/__init__.py``, ``OutputEventFilter._emit_event``),
    color stripped; None for any other event."""

    if event.get("event") != "verbose":
        return None
    return _one_line(_COLOR.sub("", str(event.get("stdout", ""))))


class Stopped:
    """Why Ansible stopped a run before any task ended: the first line it
    printed as an error (a playbook or role file that does not parse, a module
    no collection installed provides), with the file and line it named when
    that file is in the project.

    It is the one event read from stdout, so it keeps that one line, struck of
    secrets and clipped as a task's message is, and a path outside the project
    (the worker's own directories) is never said.
    """

    def __init__(self, project_dir: PurePath, secrets: Collection[str] = ()) -> None:
        self._project_dir = project_dir
        self._secrets = secrets
        self._error = ""
        self._where = ""
        self._next = False

    def read(self, event: Mapping[str, Any]) -> None:
        line = _printed(event)
        if line is None:
            return
        if not self._error:
            if line.startswith(ERROR):
                self._error = line.removeprefix(ERROR)
                self._next = True
            return
        if self._next:
            self._next = False
            if line.startswith(ORIGIN):
                self._where = self._within(line.removeprefix(ORIGIN))

    def _within(self, origin: str) -> str:
        path, _, place = origin.rpartition(":")
        path, _, line = path.rpartition(":")
        if not (line.isdigit() and place.isdigit()):
            return ""
        try:
            relative = PurePath(path).relative_to(self._project_dir)
        except ValueError:
            return ""
        if ".." in relative.parts:
            return ""
        return f"{relative.as_posix()}:{line}:{place}"

    @property
    def message(self) -> str | None:
        if not self._error:
            return None
        said = f"{self._where}: {self._error}" if self._where else self._error
        return _clip(strike(said, self._secrets), MESSAGE_LIMIT)


# The stat a playbook states its result under, with ``ansible.builtin.set_stats``.
RESULT_STAT = "result"


def stated(event: Mapping[str, Any], secrets: Collection[str] = ()) -> str | None:
    """What a playbook said it found or did, from the ``playbook_on_stats``
    event that ends its run, or None for any other event or a playbook that
    set no ``result``.

    ansible-runner carries a run's stats set with ``per_host: false`` as that
    event's ``artifact_data``. A result keyed by machine, as a playbook on
    several sets it, is said machine by machine.
    """

    data = event.get("event_data")
    if event.get("event") != "playbook_on_stats" or not isinstance(data, Mapping):
        return None
    artifact = data.get("artifact_data")
    result = artifact.get(RESULT_STAT) if isinstance(artifact, Mapping) else None
    if isinstance(result, Mapping):
        said = (
            [str(value) for value in result.values()]
            if len(result) == 1
            else [f"{host}: {value}" for host, value in sorted(result.items())]
        )
        result = "; ".join(said)
    if result is None or not _one_line(result):
        return None
    return _clip(strike(_one_line(result), secrets), MESSAGE_LIMIT)


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
