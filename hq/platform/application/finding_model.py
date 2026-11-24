"""What a finding is made of: the claim, its remedies, the rule that raises it and the estate it reads.

Every detector module builds its findings from these, and application.findings
derives, resolves and serves them. Kept apart so a detector can import its
vocabulary without importing the pipeline that collects it."""

import re
import shlex
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, NotRequired, TypedDict

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import ActionLink, read_link
from .cadence import CONTROLLER_REFRESH
from .describe import Describe
from .display_words import as_members, in_words
from .entity_links import EntityLink, link_url
from .finding_steps import Do, Press, Run, Step
from .moments import moment
from .tone import Tone
from .topology_model import Topology, TopologyNode
from .ui import ended
lazy from .capabilities import capability_spec, capability_words
lazy from .pages import command_reach

if TYPE_CHECKING:
    from .cadence import ControllerSweepCommand
    from .security import Principal


@dataclass(frozen=True, slots=True)
class FindingRemedy:
    """An existing capability, named: never a new way to change anything.

    ``intent`` is this remedy's words for the press where the command's name
    would not say it, read by every surface that offers it. ``url`` and
    ``method`` name a route that runs it on something other than its target.
    """

    capability: str
    target: str
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

    @property
    def effect(self) -> str:
        """What running it does, as its command declares; empty for a command HQ does not hold."""

        spec = capability_spec(self.capability)
        return spec.effect if spec else ""

    @property
    def reach(self) -> tuple[str, str]:
        """Where it is pressed, and how: its own route, else where its command is reached."""

        if self.url:
            return self.url, self.method
        url, answers_get = command_reach(self.capability, self.target, self.variant)
        return url, "GET" if answers_get else "POST"


# A line of what a finding rests on: its label, and its value in words, or
# the record it names as the link builder answers for it (``entity_link``),
# which every door links.
Fact = tuple[str, str | EntityLink]


class FindingEvidence(TypedDict):
    """One line of what a finding rests on."""

    label: Annotated[str, Describe("What this line of evidence is, as a problem card labels it.")]
    value: Annotated[str, Describe("What was found, most often a name or a sentence.")]
    url: Annotated[str, Describe("The page of what it names: a path in HQ, or an outside address. Empty for none.")]


def written_fact(label: str, value: str | EntityLink) -> FindingEvidence:
    """A line of evidence as every door answers it, a record with its page."""

    if isinstance(value, str):
        return {"label": label, "value": value, "url": ""}
    return {"label": label, "value": value.label, "url": link_url(value.url)}


@dataclass(frozen=True, slots=True)
class Finding:
    """One claim, the evidence for it, and what could be done about it."""

    rule: str
    subject: str
    title: str
    severity: Tone
    explanation: str
    evidence: tuple[Fact, ...] = ()
    # The commands that would fix it, as its detector names them. Derivation
    # resolves each once, into a ``Press`` among its steps for a reader who may
    # run it, and empties this.
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
    # What to do about it, in order: a command HQ runs (``Press``, which only
    # derivation writes), one the operator runs (``Run``), or what to do in
    # words (``Do``). Derivation adds its rule's ``operator_action`` where it
    # leaves none.
    steps: tuple[Step, ...] = ()
    # Why HQ can neither run a remedy nor name the exact command for this one.
    # Derivation fills it from the rule when the finding carries neither.
    no_help_reason: str = ""
    # The press that reads again what it rests on and runs its check, set by
    # derivation: a button, never a step.
    check: ActionLink | None = None
    # When this was first seen open: when its condition began, where the
    # detector read it, else where a look recorded it (application.first_seen).
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
    # How many things one finding stands for, where it counts a backlog of
    # records: what the queue's row counts, and what brings a set-aside one back.
    count: int = 0
    # News rather than a problem (a draft in hand, a deploy on its way): the
    # queue keeps it in its own section, below what needs doing.
    notice: bool = False
    # Its only remedy does not exist yet (an alert no released version fixes):
    # it stays a finding, and the queue leaves it out until one does.
    awaiting_fix: bool = False
    # A request a person decides, by its Approve or Reject: it ends only when
    # decided, so a reader can never set it aside, and a mark from before
    # does not hide it.
    held: bool = False
    # What a finding no host rule raises says of itself, where a rule would:
    # an extension's or a held request's. A host rule's finding is worded by
    # its rule alone (``dashboard.finding_rows``), which ignores these.
    # Why it is as urgent as its severity says, in one sentence.
    why: str = ""
    # What clears it, a clause after "Clears once".
    clears: str = ""
    # What kind of matter it is, as its owner heads a list of them; several
    # of one family from one source fold under it in the queue.
    family: str = ""
    # Playbook context, derived from the observed service and adopted source.
    service_unit: str = field(default="", kw_only=True)

    def __post_init__(self) -> None:
        as_members(self)
        # A card says it as written, so what it says is words, and each
        # reading's label is drawn once with its values beside it.
        for text in (self.title, self.explanation):
            in_words(text)
        for label, value in self.evidence:
            in_words(label, label=True)
            in_words(str(value))

    @property
    def one_press(self) -> bool:
        """Whether HQ does what it asks in one press: the first command it offers would run now."""

        lead = self.presses[0] if self.presses else None
        return lead is not None and bool(lead.capability) and not lead.refusal

    @property
    def presses(self) -> tuple[ActionLink, ...]:
        """The commands HQ runs for it, in the order offered."""

        return tuple(step.link for step in self.steps if isinstance(step, Press))

    @property
    def pages(self) -> tuple[ActionLink, ...]:
        """The pages that help look into it, each once by its label: its investigations, then its offers."""

        seen: dict[str, ActionLink] = {}
        for link in (*self.investigations, *self.offers):
            seen.setdefault(link.label, link)
        return tuple(seen.values())


@dataclass(frozen=True, slots=True)
class FindingRule:
    """A named claim, and how to decide and explain it.

    ``subsumes`` is what keeps a queue from tripling. A resource nothing governs
    is uncovered, not skipped, and reporting both puts one problem in front of
    an operator twice under two names.
    """

    name: str
    title: str
    severity: Tone
    detect: Callable[[FindingEstate], tuple[Finding, ...]]
    # The precise action that resolves a finding of this rule by hand. Required:
    # a finding either offers an operation through the gated queue or says
    # exactly what to do. A finding may state a more specific one.
    operator_action: str
    # Why HQ cannot resolve a finding of this rule itself: shown whenever one
    # carries no remedy and no exact command. Required, so no finding reaches
    # an operator as bare prose.
    no_help_reason: str = ""
    subsumes: tuple[str, ...] = ()
    # The card of its subject's page that draws it, where the page has one for
    # the parts it is about ("containers" on a machine's page); empty, the
    # page's own place for problems.
    card: str = ""
    # The bar a finding crosses to stand at each severity the rule raises, a
    # clause after "urgent once": "free space is under 512 MB and 3% of the
    # disk". Worded from the detector's own constants, so a card's why says
    # what was read and the bar it crossed (``attention.finding_why``).
    bars: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    # What clears its findings, where a threshold raises them, worded from the
    # same constants as its lowest bar (``card_items.Insight.clears``). The
    # ast-grep rule threshold-says-what-clears-it asks it of every rule whose
    # bars name a constant.
    clears: str = ""
    # The host section whose records a rule is about (``hq.expenses``): its
    # findings are raised under that section, and told only to a reader of
    # what the section declares. Empty for a rule about the estate, whose
    # subject gates it and which Infrastructure raises.
    domain: str = ""
    # The resource whose gate joins its findings' read gate.
    read_through: str = ""
    # Whether its detector reads HQ's own records (a model, a stored reading)
    # rather than the projection. It runs as the estate is gathered, before
    # derivation, which reads nothing of its own.
    gathered: bool = False

    def __post_init__(self) -> None:
        as_members(self)
        if not self.operator_action.strip():
            raise ValueError(f"Finding rule {self.name!r} must say how it is resolved.")
        if not self.no_help_reason.strip():
            raise ValueError(f"Finding rule {self.name!r} must say why HQ cannot resolve it itself.")
        if not self.bars.get(self.severity, "").strip():
            raise ValueError(f"Finding rule {self.name!r} must say the bar its {self.severity} findings cross (bars).")


# What each rule that reads HQ's records found, by rule (``gathered_findings``).
Gathered = Mapping[str, tuple[Finding, ...]]


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
    # Whom it was narrowed for, so a gathered rule reads only what they may.
    principal: Principal | None = None

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
        FindingRemedy(capability="tailnet.reach.allow", target=node.label),
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
    steps: NotRequired[tuple[Run | Do, ...]]


def per_connection(
    estate: FindingEstate,
    key: str,
    *,
    rule: str,
    severity: Tone,
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


def opens(label: str, url: str) -> ActionLink:
    """The page a finding's card leads with, under ``label``."""

    return read_link("open", label, url)


def distinct(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """Each of ``findings`` once by its subject, part and title, in that order:
    a detector that walks several routes to one place may word it twice."""

    found: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        found.setdefault((finding.subject, finding.part, finding.title), finding)
    return tuple(found[key] for key in sorted(found))


# A problem card's own button: read what the claim rests on again, then run
# the check against it.
CHECK_AGAIN = "Check again"
PLAYBOOK_INVESTIGATION = "playbook-investigation"
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


def machine_step(label: str, machine: str, command: str, notes: tuple[str, ...] = ()) -> tuple[Run, ...]:
    """One step that is a command on ``machine``, or none when the command
    cannot be written for it: a step with no command says nothing to run."""

    pasted = on_machine(machine, command)
    return (Run(label=label, command=pasted, notes=notes),) if pasted else ()


def journal_step(label: str, machine: str, unit: str, notes: tuple[str, ...] = ()) -> tuple[Run, ...]:
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
