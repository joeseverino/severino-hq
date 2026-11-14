"""A run's task outcomes as its report carries them: one per task and machine,
struck of secrets and held to the bridge's ``RunEvent`` bounds; and what its
playbook stated as it ended (``stated``), held to ``RunStated``'s.

An event is built from four fields of what Ansible said (the host, the task's
name, its outcome and its own one-line ``msg``), never from its whole result,
its stdout or its arguments, so what a module returned beside its message
never leaves the worker. A task marked ``no_log`` says only that it was hidden.
The one line of stdout read is the error that stops a run before any task
ends (``Stopped``), struck as a task's message is. HQ strikes the credential
shapes it knows again at the bridge's door; this is the worker's own strike,
of what it alone knows to be secret.
"""

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
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


# The stats a playbook states with ``ansible.builtin.set_stats`` that a report
# carries: its result, and its readings and preview (``RunStated``).
RESULT_STAT = "result"
READINGS_STAT = "readings"
PREVIEW_STAT = "preview"

# The bridge contract's RunStated bounds: the compact JSON of all of it, the
# records of one machine and kind (and the lines of one machine's preview),
# every text in it and a kind's name.
STATED_BYTES = 65536
RECORD_LIMIT = 200
TEXT_LIMIT = 500
KIND_LIMIT = 100

# Why a part of what a playbook stated is not reported, in the words its
# report says it in. A part is one machine's records of one kind, one
# machine's preview, or everything a stat held for a machine the run is not on.
ELSEWHERE = "for a machine it was not run on"
PAST_BOUNDS = "past the bridge's bounds"
MISSHAPEN = "not shaped as a reading or a preview"


@dataclass(frozen=True, slots=True)
class Stated:
    """What a playbook stated as its run ended: its result in a line, its
    readings (``{machine: {kind: [record]}}``) and preview (``{machine:
    [line]}``) on the run's machines alone, and how many parts were left out
    and why."""

    result: str | None
    readings: dict[str, dict[str, list[Any]]]
    preview: dict[str, list[str]]
    left_out: dict[str, int]

    def as_report(self) -> dict[str, Any]:
        """The report's ``stated``: each stat that holds anything."""

        said = {READINGS_STAT: self.readings, PREVIEW_STAT: self.preview}
        return {name: value for name, value in said.items() if value}


def struck_all(node: Any, secrets: Collection[str]) -> Any:
    """``node`` with ``secrets`` struck from every string in it, at any depth."""

    if isinstance(node, str):
        return strike(node, secrets)
    if isinstance(node, Mapping):
        return {key: struck_all(value, secrets) for key, value in node.items()}
    if isinstance(node, list):
        return [struck_all(value, secrets) for value in node]
    return node


def stated_bytes(stated: Mapping[str, Any]) -> int:
    """How many bytes ``stated`` is as the contract measures it: compact JSON in UTF-8."""

    return len(json.dumps(stated, separators=(",", ":"), ensure_ascii=False).encode())


def _texts_within(node: Any) -> bool:
    """Whether every text in ``node``, a member's name or a value, is within ``TEXT_LIMIT``."""

    if isinstance(node, str):
        return len(node) <= TEXT_LIMIT
    if isinstance(node, Mapping):
        return all(isinstance(key, str) and len(key) <= TEXT_LIMIT and _texts_within(v) for key, v in node.items())
    if isinstance(node, list):
        return all(_texts_within(value) for value in node)
    return True


class _Parts:
    """The parts of what a playbook stated a report carries, each kept whole or
    left out whole: a part past a bound is never cut down, since a reading cut
    short would say something false."""

    def __init__(self, hosts: Collection[str], secrets: Collection[str]) -> None:
        self._hosts = hosts
        self._secrets = secrets
        self.left_out: dict[str, int] = {}

    def leave_out(self, why: str, parts: int = 1) -> None:
        if parts:
            self.left_out[why] = self.left_out.get(why, 0) + parts

    def by_machine(self, stat: Any) -> dict[str, Any]:
        """A stat's value on each of the run's machines; any other machine's left out."""

        if stat is None:
            return {}
        if not isinstance(stat, Mapping):
            self.leave_out(MISSHAPEN)
            return {}
        mine: dict[str, Any] = {}
        for machine, value in stat.items():
            if machine in self._hosts:
                mine[machine] = value
            else:
                self.leave_out(ELSEWHERE)
        return mine

    def kept(self, values: Any, shaped: Callable[[Any], bool]) -> list[Any] | None:
        """``values`` struck of secrets, or None when it is left out."""

        if not isinstance(values, list) or not all(shaped(value) for value in values):
            self.leave_out(MISSHAPEN)
            return None
        struck: list[Any] = struck_all(values, self._secrets)
        if len(struck) > RECORD_LIMIT or not _texts_within(struck):
            self.leave_out(PAST_BOUNDS)
            return None
        return struck

    def readings(self, stat: Any) -> dict[str, dict[str, list[Any]]]:
        found: dict[str, dict[str, list[Any]]] = {}
        for machine, kinds in self.by_machine(stat).items():
            if not isinstance(kinds, Mapping):
                self.leave_out(MISSHAPEN)
                continue
            for kind, records in kinds.items():
                if len(kind) > KIND_LIMIT:
                    self.leave_out(PAST_BOUNDS)
                    continue
                kept = self.kept(records, lambda record: isinstance(record, Mapping))
                if kept is not None:
                    found.setdefault(machine, {})[kind] = kept
        return found

    def preview(self, stat: Any) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for machine, lines in self.by_machine(stat).items():
            kept = self.kept(lines, lambda line: isinstance(line, str))
            if kept is not None:
                found[machine] = kept
        return found


def stated(event: Mapping[str, Any], hosts: Collection[str] = (), secrets: Collection[str] = ()) -> Stated | None:
    """What a playbook stated as it ended, from the ``playbook_on_stats``
    event that ends its run, or None for any other event.

    ansible-runner carries a run's stats set with ``per_host: false`` as that
    event's ``artifact_data``, each machine's merged into one where the stat
    is keyed by machine. Only ``result``, ``readings`` and ``preview`` are
    read; any other stat is dropped. What it states about a machine that is
    not one of ``hosts``, the run's, is left out, so a run states nothing
    about a machine it was not asked on; so is a part that is misshapen or
    past a bound of ``RunStated``, and everything when all of it is past
    ``STATED_BYTES``. What is kept is struck of secrets.
    """

    data = event.get("event_data")
    if event.get("event") != "playbook_on_stats" or not isinstance(data, Mapping):
        return None
    artifact = data.get("artifact_data")
    artifact = artifact if isinstance(artifact, Mapping) else {}
    parts = _Parts(hosts, secrets)
    readings = parts.readings(artifact.get(READINGS_STAT))
    preview = parts.preview(artifact.get(PREVIEW_STAT))
    found = Stated(_result(artifact.get(RESULT_STAT), secrets), readings, preview, parts.left_out)
    if stated_bytes(found.as_report()) <= STATED_BYTES:
        return found
    parts.leave_out(PAST_BOUNDS, sum(map(len, readings.values())) + len(preview))
    return Stated(found.result, {}, {}, parts.left_out)


def _result(result: Any, secrets: Collection[str]) -> str | None:
    """A playbook's ``result`` in one line; a result keyed by machine, as a
    playbook on several sets it, is said machine by machine."""

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
