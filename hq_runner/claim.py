"""A claimed run, read from the bridge's answer, and what it binds the worker to.

What runs is decided here and only here: the procedure the claim names, at the
revision it was reviewed at, as this worker has it installed. The playbook and
project come from that installation, the hosts and extra-vars from the run's
input as HQ validated and approved it; nothing in a claim names a path, a
module, a callback or an Ansible option.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUN_KIND = "automation.operation"
RUN_ACTION = "run"
# What a run worker claims: the one kind:action it has a handler for.
CAPABILITY = f"{RUN_KIND}:{RUN_ACTION}"
_REVISION = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """A claim this worker will not run, and why; reported, never run."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True, slots=True)
class Procedure:
    """One procedure as this worker has it installed: the reviewed commit its
    project is checked out at, and its playbook within that project."""

    revision: str
    project_dir: Path
    playbook: str


@dataclass(frozen=True, slots=True)
class Run:
    """A claimed run of one installed procedure."""

    operation: str
    generation: int
    procedure_id: str
    procedure: Procedure
    hosts: tuple[str, ...]
    parameters: Mapping[str, Any]
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class Claimed:
    """What a claim answered: the operation's id and the run's generation,
    which its report names, before anything about it is trusted to run."""

    operation: str
    generation: int
    pending: Mapping[str, Any]


def claimed(pending: Mapping[str, Any]) -> Claimed | None:
    """The operation a claim answered, or None when nothing was waiting."""

    operation = pending.get("operation")
    if operation is None:
        return None
    if not isinstance(operation, Mapping) or not isinstance(operation.get("id"), str):
        raise Refused("Unrunnable", "The claim's answer names no operation id.")
    run_input = pending.get("input")
    generation = run_input.get("generation") if isinstance(run_input, Mapping) else None
    if not isinstance(generation, int) or isinstance(generation, bool):
        # Without the generation its report cannot be accepted, so there is
        # nothing to say back; the lease lapses and HQ ends it as unknown.
        generation = -1
    return Claimed(operation["id"], generation, pending)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def bind(claim: Claimed, installed: Mapping[str, Procedure]) -> Run:
    """The run ``claim`` names, bound to what this worker has installed, or
    ``Refused`` before anything runs."""

    pending = claim.pending
    operation = pending["operation"]
    resource = pending.get("resource")
    run_input = pending.get("input")
    if operation.get("action") != RUN_ACTION or not isinstance(resource, Mapping) or resource.get("kind") != RUN_KIND:
        raise Refused("Unrunnable", "The claim is not a run of an automation procedure.")
    if not isinstance(run_input, Mapping) or claim.generation < 0:
        raise Refused("Unrunnable", "The claim carries no run input, so there is nothing it was asked to run.")
    spec = resource.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    procedure_id = _text(run_input.get("operation"))
    revision = _text(run_input.get("revision"))
    if procedure_id != _text(spec.get("operation")) or revision != _text(spec.get("revision")):
        raise Refused("Unrunnable", "The run's input names another procedure or revision than its record.")
    if not _REVISION.fullmatch(revision):
        raise Refused("Unrunnable", "The run names no reviewed commit.")
    procedure = installed.get(procedure_id)
    if procedure is None:
        raise Refused("NotInstalled", f"This worker has no procedure {procedure_id} installed.")
    if procedure.revision != revision:
        raise Refused(
            "RevisionMismatch",
            f"The run was reviewed at {revision[:12]}, and this worker has {procedure.revision[:12]} installed.",
        )
    hosts = run_input.get("hosts")
    parameters = run_input.get("parameters")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(host, str) and host for host in hosts):
        raise Refused("Unrunnable", "The run names no machine to run on.")
    if not isinstance(parameters, Mapping):
        raise Refused("Unrunnable", "The run's parameters are not an object.")
    timeout = spec.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        raise Refused("Unrunnable", "The procedure's record declares no timeout.")
    return Run(
        operation=claim.operation,
        generation=claim.generation,
        procedure_id=procedure_id,
        procedure=procedure,
        hosts=tuple(hosts),
        parameters=dict(parameters),
        timeout_seconds=timeout,
    )
