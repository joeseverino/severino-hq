"""What a finding is made of: the claim, its remedies, the rule that raises it and the estate it reads.

Every detector module builds its findings from these, and application.findings
derives, resolves and serves them. Kept apart so a detector can import its
vocabulary without importing the pipeline that collects it."""

import json
import re
import shlex
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, NotRequired, TypedDict

from pydantic import ConfigDict, with_config

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import ActionLink
from .command_fields import Describe
from .core_capabilities import CONTROLLER_REFRESH
from .timestamps import moment
from .topology_model import Topology, TopologyNode
from .ui import ended
from .workflows import WorkflowPlan
lazy from .capabilities import capability_words

if TYPE_CHECKING:
    from .cadence import ControllerSweepCommand


@dataclass(frozen=True, slots=True)
class FindingRemedy:
    """An existing capability, named: never a new way to change anything.

    Said in the capability's words, or one of its declared ``variant``s.
    ``intent`` is this remedy's own words for what the press does here, where
    the command's name would not say it ("Mark it as running only
    sometimes" through the command that changes a record): declared once,
    here, and read by every surface that offers the remedy.
    """

    capability: str
    target: str
    effect: str = ""
    intent: str = ""
    variant: str = ""
    url: str = ""
    method: str = "GET"
    # Whether the controller would run this unattended. Left False here: this
    # module proposes, and the thing that already schedules automatic work is
    # the only correct place for anything else.
    auto: bool = False

    @property
    def label(self) -> str:
        return self.intent or capability_words(self.capability, self.variant)


@dataclass(frozen=True, slots=True)
class Finding:
    """One claim, the evidence for it, and what could be done about it."""

    rule: str
    subject: str
    title: str
    severity: str
    explanation: str
    evidence: tuple[tuple[str, str], ...] = ()
    remedies: tuple[FindingRemedy, ...] = ()
    # Safe reads that help with it: the detector's own (a container's upgrade
    # plan), then the subject node's. Delivery adapters render these; they do
    # not rediscover or filter them.
    offers: tuple[ActionLink, ...] = ()
    # Canonical graph investigations derived once from the finding subject.
    investigations: tuple[ActionLink, ...] = ()
    # A kind rather than a node, for a claim about a whole class. The estate
    # keeps its honesty: no synthetic node is invented to hang this on.
    scope: str = ""
    # Kinds this higher-order claim explains. They remain exact machine facts,
    # while a page can lead with the shared cause once.
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
    # The page of what it is about, as the topology names it. Derivation sets
    # it beside the offers, so a card links its title without the topology.
    subject_link: ActionLink | None = None
    # What a reader must hold to be told this, derived beside the subject link
    # (findings.read_gate). A detector sets it only for evidence that names
    # what its subject and scope do not gate, such as a posture check's.
    read_capabilities: tuple[str, ...] = ()
    # The one thing on its subject it is about, where the subject holds many:
    # a container's name on its machine. A page about one of them alone tells
    # its own problems from its neighbors' by it.
    part: str = ""
    # The read its verify step asks the controller for, set beside that step's
    # link, whose target alone cannot say whether it names a connection or a
    # kind; a door that calls rather than links sends it as the payload.
    read_now: ControllerSweepCommand | None = None
    # When the reading it rests on was taken, where one reading dates it, and
    # that reading's kind: a card says how old what it claims is once it is
    # out of date.
    observed_at: datetime | None = None
    reads: str = ""
    # The page a card leads with, where its subject's own page is not it: the
    # certificate to renew, the containers to upgrade.
    primary: ActionLink | None = None


@dataclass(frozen=True, slots=True)
class FindingRule:
    """A named claim, and how to decide and explain it.

    ``subsumes`` is what keeps a queue from tripling. A resource nothing governs
    is uncovered, not skipped, and reporting both puts one problem in front of
    an operator twice under two names.
    """

    name: str
    title: str
    severity: str
    detect: Callable[[FindingEstate], tuple[Finding, ...]]
    # The precise action that resolves a finding of this rule by hand. Required:
    # a finding either offers an operation through the gated queue or says
    # exactly what to do. A finding may state a more specific one.
    operator_action: str
    # Why HQ cannot resolve a finding of this rule itself: shown whenever one
    # carries no remedy and no exact command. Required, so no finding reaches
    # an operator as bare prose (application.item_help).
    no_help_reason: str = ""
    subsumes: tuple[str, ...] = ()
    # The card of its subject's page that draws it, where the page has one for
    # the parts it is about ("containers" on a machine's page); empty, the
    # page's own place for problems.
    card: str = ""

    def __post_init__(self) -> None:
        if not self.operator_action.strip():
            raise ValueError(f"Finding rule {self.name!r} must say how it is resolved.")
        if not self.no_help_reason.strip():
            raise ValueError(f"Finding rule {self.name!r} must say why HQ cannot resolve it itself.")


@dataclass(frozen=True, slots=True)
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


def open_the_path(node: TopologyNode) -> tuple[FindingRemedy, ...]:
    """Amend the policy, and look again once it is amended.

    The capability is handed the resource, not a path: which one to open is
    re-derived from what was observed, so a remedy can never carry an access
    rule of its own. The amendment lands on a gated kind, so a person still
    consents before the tailnet changes.
    """

    return (
        FindingRemedy(capability="tailnet.reach.allow", target=node.label, effect="infrastructure_change"),
        *reconcile_remedy(node),
    )


def reconcile_remedy(node: TopologyNode) -> tuple[FindingRemedy, ...]:
    """The one capability that makes the live record match HQ's config, where
    the kind allows it: it creates a record that is missing and corrects one
    that differs."""

    provider = PROVIDERS.get(node.kind_key)
    policy = provider.actions.get("reconcile") if provider else None
    if policy is not None and policy.mode == "locked":
        return ()
    return (FindingRemedy(capability="infrastructure.resource.reconcile", target=node.label),)


def is_observable(kind_key: str) -> bool:
    """Whether anything can ever produce an observation of this kind.

    Judging by the sweep interval a kind no sweep visits (a printer, an offline
    CA, a delivery target, a certificate observed only when issued or
    installed) asks for a reading that cannot arrive, so its claim could never
    clear; a certificate's real risk, expiry, is reported elsewhere. Read from
    the provider's ``unobserved_reason``, which a test joins to the collectors.
    """

    provider = PROVIDERS.get(kind_key)
    return not (provider and provider.unobserved_reason)


def fact_values(node, key: str) -> tuple[str, ...]:
    """The non-empty values a node carries under one fact key."""

    return tuple(value for fact, value in node.facts if fact == key and value)


def connections_carrying(estate: FindingEstate, key: str) -> Iterator[tuple[TopologyNode, tuple[str, ...]]]:
    """Each connection that carries values under one fact key, with those values.

    The walk every connection-fact finding starts from: a connection whose
    reading said nothing under the key raises nothing.
    """

    for node in estate.nodes():
        if node.kind == "connection" and (values := fact_values(node, key)):
            yield node, values


class Said(TypedDict):
    """What a connection-fact finding says of one connection; the rest is its rule's."""

    title: str
    explanation: str
    remedies: NotRequired[tuple[FindingRemedy, ...]]
    steps: NotRequired[tuple[OperatorStep, ...]]


def per_connection(
    estate: FindingEstate,
    key: str,
    *,
    rule: str,
    severity: str,
    shown_as: str,
    says: Callable[[TopologyNode, tuple[str, ...]], Said],
) -> tuple[Finding, ...]:
    """A finding of ``rule`` for each connection that carries values under ``key``, in title order.

    Each value is a line of evidence, labeled ``shown_as``, and ``says`` words
    the title, the explanation and anything offered.
    """

    found = (
        Finding(
            rule=rule,
            subject=node.id,
            severity=severity,
            evidence=tuple((shown_as, value) for value in values),
            **says(node, values),
        )
        for node, values in connections_carrying(estate, key)
    )
    return tuple(sorted(found, key=lambda finding: finding.title))


# A finding worded while the topology is derived, which may read, carried on
# its subject's node so its rule reads it back and deriving findings costs no
# query.
CARRIED = "finding"


def carry(nodes: dict[str, TopologyNode], findings: Iterable[Finding]) -> None:
    """Each of ``findings`` as a fact on its subject's node.

    A subject with no node of its own (a name only a gate mentions) rides on
    the first node: the rules read every node, so where it rides only has to
    exist.
    """

    for finding in findings:
        anchor = finding.subject if finding.subject in nodes else next(iter(nodes), "")
        if anchor:
            node = nodes[anchor]
            nodes[anchor] = replace(node, facts=(*node.facts, (CARRIED, _encoded(finding))))


def _encoded(finding: Finding) -> str:
    return json.dumps(
        {
            "rule": finding.rule,
            "subject": finding.subject,
            "title": finding.title,
            "severity": finding.severity,
            "explanation": finding.explanation,
            "part": finding.part,
            "evidence": [list(pair) for pair in finding.evidence],
            "remedies": [
                [remedy.capability, remedy.target, remedy.intent, remedy.variant, remedy.url, remedy.method]
                for remedy in finding.remedies
            ],
            "steps": [[step.label, step.command, list(step.notes)] for step in finding.steps],
            "offers": [[offer.name, offer.label, offer.url, offer.reason] for offer in finding.offers],
            "observed_at": finding.observed_at.isoformat() if finding.observed_at else "",
            "reads": finding.reads,
            "primary": [finding.primary.label, finding.primary.url] if finding.primary else [],
        }
    )


def _decoded(value: str) -> Finding:
    item = json.loads(value)
    return Finding(
        rule=item["rule"],
        subject=item["subject"],
        title=item["title"],
        severity=item["severity"],
        explanation=item["explanation"],
        part=item["part"],
        evidence=tuple(tuple(pair) for pair in item["evidence"]),
        remedies=tuple(
            FindingRemedy(capability=capability, target=target, intent=intent, variant=variant, url=url, method=method)
            for capability, target, intent, variant, url, method in item["remedies"]
        ),
        steps=tuple(
            OperatorStep(label=label, command=command, notes=tuple(notes)) for label, command, notes in item["steps"]
        ),
        offers=tuple(
            ActionLink(name, label, "read", url, reason=reason) for name, label, url, reason in item["offers"]
        ),
        observed_at=moment(item["observed_at"]),
        reads=item["reads"],
        primary=opens(*item["primary"]) if item["primary"] else None,
    )


def opens(label: str, url: str) -> ActionLink:
    """The page a finding's card leads with, under ``label``."""

    return ActionLink("open", label, "read", url)


def carried(rule: str) -> Callable[[FindingEstate], tuple[Finding, ...]]:
    """The detector of ``rule``: the findings of it the topology carries, each once, in title order."""

    def detect(estate: FindingEstate) -> tuple[Finding, ...]:
        found = {}
        for node in estate.nodes():
            for value in fact_values(node, CARRIED):
                finding = _decoded(value)
                if finding.rule == rule:
                    found.setdefault((finding.subject, finding.part, finding.title), finding)
        return tuple(found[key] for key in sorted(found))

    return detect


# Its own config, so it is written one way under every answer that holds it.
@with_config(ConfigDict(extra="forbid"))
@dataclass(frozen=True, slots=True)
class OperatorStep:
    """A command an operator runs on their own machine. HQ never runs it."""

    label: Annotated[str, Describe("What the step does, in a sentence; all there is to it when it names no command.")]
    command: Annotated[
        str,
        Describe("The exact command to paste into a shell on the machine it names; empty for a step said in words."),
    ] = ""
    notes: Annotated[tuple[str, ...], Describe("What to know before or after running it, a sentence each.")] = ()


# A problem card's own button: read what the claim rests on again, then run
# the check against it.
CHECK_AGAIN = "Check again"
# What a step says last when a fresh read is what confirms the fix. A problem
# card carries Check again; a connection's own page carries the refresh.
THEN_CHECK_AGAIN = f"Then press {CHECK_AGAIN}."
THEN_REFRESH = f"Then press {CONTROLLER_REFRESH.words()}."

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


def machine_step(label: str, machine: str, command: str, notes: tuple[str, ...] = ()) -> tuple[OperatorStep, ...]:
    """One step that is a command on ``machine``, or none when the command
    cannot be written for it: a step with no command says nothing to run."""

    pasted = on_machine(machine, command)
    return (OperatorStep(label=label, command=pasted, notes=notes),) if pasted else ()


def journal_step(label: str, machine: str, unit: str, notes: tuple[str, ...] = ()) -> tuple[OperatorStep, ...]:
    """The step that reads a background job's log on its machine."""

    return machine_step(label, machine, f"journalctl -u {shlex.quote(unit)} -n 50 --no-pager", notes)


def cannot_run_commands(machine: str = "") -> str:
    """Why a fix that is a command on a machine is the owner's to run."""

    return ended(f"HQ cannot run commands on {machine or 'the machine'}")


CANNOT_EDIT_COMPOSE = "HQ cannot edit compose files."
# A machine page's card of its containers, for a rule about one of them, and
# its card of its tailnet device, for a rule about that.
CONTAINERS_CARD = "containers"
TAILNET_CARD = "tailnet"


def parse_stamp(value: str) -> datetime | None:
    """A fact's or node's stamp, as it was written: naive stays naive."""

    return moment(value, naive="keep")
