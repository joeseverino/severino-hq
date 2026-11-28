"""What a finding is made of: the claim, its fixes, the rule that raises it and the estate it reads.

Every detector module builds its findings from these, and application.findings
derives, resolves and serves them. Kept apart so a detector can import its
vocabulary without importing the pipeline that collects it."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, NotRequired, TypedDict

from hq.domains.control_plane.providers import PROVIDERS

from .action_links import ActionLink, read_link
from .describe import Describe
from .display_words import Code, as_members, in_words
from .entity_links import EntityLink, link_url
from .finding_steps import Advice, Does, Press, Step
from .moments import moment
from .non_empty import RequiredText, holds_something
from .tone import Tone
from .topology_model import Topology, TopologyNode
from .ui import ended

if TYPE_CHECKING:
    from .asks import Standing
    from .cadence import ControllerSweepCommand
    from .security import Principal


# A line of what a finding rests on: its label, and its value in words,
# what a machine wrote as it reads (``Code``, a digest or a path), or the
# record it names as the link builder answers for it (``entity_link``), which
# every door links.
Fact = tuple[str, str | EntityLink]


class FindingEvidence(TypedDict):
    """One line of what a finding rests on."""

    label: Annotated[str, Describe("What this line of evidence is, as a problem card labels it.")]
    value: Annotated[str, Describe("What was found, most often a name or a sentence.")]
    url: Annotated[str, Describe("The page of what it names: a path in HQ, or an outside address. Empty for none.")]
    code: Annotated[bool, Describe("Whether the value is what a machine wrote (a digest, a path), to read exactly.")]


def matter_key(rule: str, about: str, part: str = "") -> str:
    """What a finding is about, as its item in the queue is keyed: its rule,
    its subject (else its scope), and the one thing on that subject it names."""

    key = f"finding:{rule}:{about}"
    # Two containers on one machine are two matters, each keyed by its own.
    return f"{key}:{part}" if part else key


@dataclass(frozen=True, slots=True)
class LookInto:
    """What a read-only playbook that investigates a finding is run with: the
    machine, where its subject is none, and each input HQ fills by its name
    where the playbook declares one (docs/Architecture.md, "Declaring a
    playbook"). A value is left empty where the finding names several."""

    machine: str = ""
    # A systemd service by its exact name, its ``.service`` suffix included.
    service_unit: str = ""
    # A filesystem by where it is mounted.
    mount: str = ""

    @property
    def inputs(self) -> dict[str, str]:
        """The inputs it fills, by the name a playbook declares each under."""

        return {name: value for name, value in (("service_unit", self.service_unit), ("mount", self.mount)) if value}


@dataclass(frozen=True, slots=True)
class Notice:
    """What makes a notice new: the occurrence it tells of, such as the day of
    the last loaded leg session or the commit on its way to production.

    A dismissed notice stays gone until this moves (``action_items.item_revision``),
    so its wording, its tone and its count never bring it back.
    """

    occurrence: date | RequiredText

    def __post_init__(self) -> None:
        holds_something(self)

    @property
    def written(self) -> str:
        return self.occurrence.isoformat() if isinstance(self.occurrence, date) else self.occurrence


def written_fact(label: str, value: str | EntityLink) -> FindingEvidence:
    """A line of evidence as every door answers it, a record with its page."""

    if isinstance(value, str):
        return {"label": label, "value": value, "url": "", "code": isinstance(value, Code)}
    return {"label": label, "value": value.label, "url": link_url(value.url), "code": False}


@dataclass(frozen=True, slots=True)
class Finding:
    """One claim, the evidence for it, and what could be done about it."""

    rule: str
    subject: str
    title: str
    severity: Tone
    # Why it matters, in plain words: every finding says its reason, so none
    # reaches a person as a title alone.
    explanation: RequiredText
    evidence: tuple[Fact, ...] = ()
    # What to do about it, in order, as its detector says: a command HQ runs
    # (``Does``), one a person runs by hand and why (``ByHand``), or what to
    # do in words (``Do``). Derivation resolves it into ``steps`` and empties it.
    fixes: tuple[Advice, ...] = ()
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
    # What a reader is shown to do about it, from ``fixes``: derivation writes
    # each ``Does`` as a press for a reader who may run it, else its
    # ``by_hand``, adding its rule's ``operator_action`` where it leaves none.
    steps: tuple[Step, ...] = ()
    # Why HQ can neither run a remedy nor name the exact command for this one.
    # Derivation fills it from the rule when the finding carries neither.
    no_help_reason: str = ""
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
    # What to read again so its check runs on fresh data, set by derivation
    # where its reader may ask for it; the queue's Refresh asks it.
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
    # News or advice rather than a problem (a draft in hand, a deploy on its
    # way, a coaching tip), and the occurrence it tells of: the queue keeps it
    # in its own section, below what needs doing, and out of every count.
    notice: Notice | None = None
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
    # Where and on what a playbook that investigates it runs.
    look_into: LookInto = field(default=LookInto(), kw_only=True)
    # How the work HQ does on it now stands (a press's run under way), with
    # its page: its row says it and asks again until that work ends, so it
    # says where it stands without a refresh.
    under_way: Standing | None = field(default=None, kw_only=True)
    # The findings it follows from, each by its key (``matter_key``), as the
    # rule that knows the dependency declares them: while one stands, this
    # stands beneath it (``finding_fold``).
    follows: tuple[str, ...] = field(default=(), kw_only=True)
    # What follows from it, set by derivation alone: each said beneath it, never alone.
    consequences: tuple[Finding, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        as_members(self)
        holds_something(self)
        if self.fixes and not self.steps:
            # Before derivation, or where none comes (a page's own check), a
            # reader is shown what needs no one's authority: each command HQ
            # runs said by its by_hand, and the rest as written.
            shown = tuple(fix.by_hand if isinstance(fix, Does) else fix for fix in self.fixes)
            object.__setattr__(self, "steps", tuple(step for step in shown if step is not None))
        # A card says it as written, so what it says is words, and each
        # reading's label is drawn once with its values beside it.
        in_words(self.title, "Finding.title")
        in_words(self.explanation, "Finding.explanation")
        for label, value in self.evidence:
            in_words(label, "Finding.evidence", label=True)
            in_words(value if isinstance(value, str) else value.label, "Finding.evidence")

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
    operator_action: RequiredText
    # Why HQ cannot resolve a finding of this rule itself: shown whenever one
    # carries no remedy and no exact command. Required, so no finding reaches
    # an operator as bare prose.
    no_help_reason: RequiredText = ""
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
        holds_something(self)
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


def open_the_path(node: TopologyNode) -> tuple[Does, ...]:
    """Amend the policy, and look again once it is amended.

    The capability is handed the resource, not a path: which one to open is
    re-derived from what was observed, so a remedy can never carry an access
    rule of its own. The amendment lands on a gated kind, so a person still
    consents before the tailnet changes.
    """

    return (
        Does("tailnet.reach.allow", node.label),
        *reconcile_remedy(node),
    )


def reconcile_remedy(node: TopologyNode) -> tuple[Does, ...]:
    """The one capability that makes the live record match HQ's config, where
    the kind allows it: it creates a record that is missing and corrects one
    that differs."""

    provider = PROVIDERS.get(node.kind_key)
    policy = provider.actions.get("reconcile") if provider else None
    if policy is not None and policy.mode == "locked":
        return ()
    return (Does("infrastructure.resource.reconcile", node.label),)


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
    explanation: RequiredText
    fixes: NotRequired[tuple[Advice, ...]]
    look_into: NotRequired[LookInto]


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


PLAYBOOK_INVESTIGATION = "playbook-investigation"


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
