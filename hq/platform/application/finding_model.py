"""What a finding is made of: the claim, its remedies, the rule that raises it and the estate it reads.

Every detector module builds its findings from these, and application.findings
derives, resolves and serves them. Kept apart so a detector can import its
vocabulary without importing the pipeline that collects it."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import ActionLink
from .timestamps import moment
from .topology_model import Topology, TopologyNode
from .workflows import WorkflowPlan


@dataclass(frozen=True)
class Remedy:
    """An existing capability, named: never a new way to change anything."""

    capability: str
    target: str
    label: str
    effect: str
    url: str = ""
    method: str = "GET"
    # Whether the controller would run this unattended. Left False here: this
    # module proposes, and the thing that already schedules automatic work is
    # the only correct place for anything else.
    auto: bool = False


@dataclass(frozen=True)
class Finding:
    """One claim, the evidence for it, and what could be done about it."""

    rule: str
    subject: str
    title: str
    severity: str
    explanation: str
    evidence: tuple[tuple[str, str], ...] = ()
    remedies: tuple[Remedy, ...] = ()
    # Safe, already-authorized read workflows emitted by the subject node.
    # Delivery adapters render these; they do not rediscover or filter them.
    offers: tuple[ActionLink, ...] = ()
    # Canonical graph investigations derived once from the finding subject.
    investigations: tuple[ActionLink, ...] = ()
    # A kind rather than a node, for a claim about a whole class. The estate
    # keeps its honesty: no synthetic node is invented to hang this on.
    scope: str = ""
    # Kinds this higher-order claim explains. They remain exact machine facts,
    # while an effortless surface can lead with the shared cause once.
    affected_scopes: tuple[str, ...] = ()
    workflow: WorkflowPlan | None = None
    # Commands an operator runs on their own machine; HQ never runs them.
    steps: tuple[OperatorStep, ...] = ()
    # Why HQ can neither run a remedy nor name the exact command for this one.
    # Derivation fills it from the rule when the finding carries neither.
    no_help_reason: str = ""
    # When this was first seen open, where a look recorded it
    # (application.first_seen). Derivation leaves it unset.
    since: datetime | None = None


@dataclass(frozen=True)
class FindingRule:
    """A named claim, and how to decide and explain it.

    ``subsumes`` is what keeps a queue from tripling. A resource nothing governs
    is uncovered, not skipped, and reporting both puts one problem in front of
    an operator twice under two names.
    """

    name: str
    title: str
    severity: str
    detect: Callable[["FindingEstate"], tuple[Finding, ...]]
    # The precise action that resolves a finding of this rule by hand. Required:
    # a finding either offers an operation through the gated queue or says
    # exactly what to do. A finding may state a more specific one.
    operator_action: str
    # Why HQ cannot resolve a finding of this rule itself: shown whenever one
    # carries no remedy and no exact command. Required, so no finding reaches
    # an operator as bare prose (application.item_help).
    no_help_reason: str = ""
    subsumes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.operator_action.strip():
            raise ValueError(f"Finding rule {self.name!r} must say how it is resolved.")
        if not self.no_help_reason.strip():
            raise ValueError(
                f"Finding rule {self.name!r} must say why HQ cannot resolve it itself."
            )


@dataclass(frozen=True)
class FindingEstate:
    """The projection plus the two indices every rule wants, built once."""

    topology: Topology
    now: datetime
    latest_by_kind: dict[str, datetime]
    observed: dict[str, datetime]
    governed: frozenset[str]
    # Every kind that has a managed declaration, and how many. A kind absent
    # from `latest_by_kind` but present here has never been observed at all.
    declared_kinds: frozenset[str]
    declared_counts: dict[str, int]
    controllers_by_kind: dict[str, frozenset[str]]

    def nodes(self) -> tuple[TopologyNode, ...]:
        return self.topology.nodes


def open_the_path(node: TopologyNode) -> tuple[Remedy, ...]:
    """Amend the policy, and look again once it is amended.

    The capability is handed the resource, not a path: which one to open is
    re-derived from what was observed, so a remedy can never carry an access
    rule of its own. The amendment lands on a gated kind, so a person still
    consents before the tailnet changes.
    """

    return (
        Remedy(
            capability="tailnet.reach.allow",
            target=node.label,
            label="Allow it in the tailnet policy",
            effect="infrastructure_change",
        ),
        *reconcile_remedy(node),
    )


def reconcile_remedy(node: TopologyNode) -> tuple[Remedy, ...]:
    """The one capability that answers "go and look again", where the kind allows it."""

    provider = PROVIDERS.get(node.kind_key)
    policy = provider.actions.get("reconcile") if provider else None
    if policy is not None and policy.mode == "locked":
        return ()
    return (
        Remedy(
            capability="infrastructure.reconcile",
            target=node.label,
            label="Apply again",
            effect="",
        ),
    )


def is_observable(kind_key: str) -> bool:
    """Whether anything can ever produce an observation of this kind.

    A printer, a network, an offline CA, a certificate delivery target: HQ was
    told about them, nothing adopts them from a sweep and no controller acts on
    them. Judging one by when it was last observed asks for a reading that
    cannot exist, so the claim it raises can never be cleared.

    This rule measures against the sweep interval, so it only means anything
    for a kind a sweep visits. A certificate is observed when an operation
    issues or installs it, which is nothing like every sixty seconds: judged
    on that cadence it is permanently overdue and no sweep or reconcile can
    ever settle it. Its real risk, expiry, is reported where it belongs.

    `unobserved_reason` is the provider's own statement that no collector
    sweeps it, and the collector registry is joined to it by test, so this is
    read rather than derived a second time.
    """

    provider = PROVIDERS.get(kind_key)
    return not (provider and provider.unobserved_reason)


def fact_values(node, key: str) -> tuple[str, ...]:
    """The non-empty values a node carries under one fact key."""

    return tuple(value for fact, value in node.facts if fact == key and value)


def built_findings(fields: tuple[dict[str, Any], ...]) -> tuple[Finding, ...]:
    return tuple(Finding(**item) for item in fields)


@dataclass(frozen=True)
class OperatorStep:
    """A command an operator runs on their own machine. HQ never runs it."""

    label: str
    command: str = ""
    notes: tuple[str, ...] = ()


# What a step says last when a fresh read is what confirms the fix. A problem
# card carries Check again; a connection's own page carries Read now.
THEN_CHECK_AGAIN = "Then press Check again."
THEN_READ_NOW = "Then press Read now."

# A machine name a shell takes as one word.
_MACHINE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def on_machine(machine: str, command: str) -> str:
    """``command`` as it is pasted on the owner's own machine: over ssh, as
    root on ``machine``. "" when the name is not one a shell takes as one word,
    so a step never carries a command built on a guess."""

    if not _MACHINE_NAME.match(machine or "") or not command.strip():
        return ""
    escaped = re.sub(r'(["\\$`])', r"\\\1", command.strip())
    return f'ssh {machine} "sudo {escaped}"'


def machine_step(
    label: str, machine: str, command: str, notes: tuple[str, ...] = ()
) -> tuple[OperatorStep, ...]:
    """One step that is a command on ``machine``, or none when the command
    cannot be written for it: a step with no command says nothing to run."""

    pasted = on_machine(machine, command)
    return (OperatorStep(label=label, command=pasted, notes=notes),) if pasted else ()


def journal_step(label: str, machine: str, unit: str, notes: tuple[str, ...] = ()) -> tuple[OperatorStep, ...]:
    """The step that reads a background job's log on its machine."""

    return machine_step(label, machine, f"journalctl -u {shlex.quote(unit)} -n 50 --no-pager", notes)


def cannot_run_commands(machine: str = "") -> str:
    """Why a fix that is a command on a machine is the owner's to run."""

    return f"HQ cannot run commands on {machine or 'the machine'}."


CANNOT_EDIT_COMPOSE = "HQ cannot edit compose files."


def parse_stamp(value: str) -> datetime | None:
    """A fact's or node's stamp, as it was written: naive stays naive."""

    return moment(value, naive="keep")
