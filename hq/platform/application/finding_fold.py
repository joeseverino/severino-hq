"""Which raised findings give way to another, so one problem reaches a person once.

A rule that ``subsumes`` another takes that rule's findings off the subject,
scope or kind it speaks for. A finding that ``follows`` from another that
stands is said beneath it, as one of its ``consequences``: the cause is the one
item, at its own severity, and nothing that follows from it stands beside it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import replace

from .entity_links import EntityLink
from .finding_model import Fact, Finding, FindingRule, matter_key
from .topology_model import TopologyNode

# How each consequence is said beneath its cause, and its reason beside it.
CAUSES = "Causes"
WHY = "Why"


def finding_key(finding: Finding) -> str:
    """What a finding is about, as its item in the queue is keyed."""

    return matter_key(finding.rule, finding.subject or finding.scope, finding.part)


def finding_scopes(finding: Finding) -> tuple[str, ...]:
    return ((finding.scope,) if finding.scope else ()) + finding.affected_scopes


class Subsumed:
    """What each rule's raised findings silence of the rules it subsumes."""

    def __init__(self, rules: Iterable[FindingRule], raised: Mapping[str, tuple[Finding, ...]]) -> None:
        self.kinds: set[str] = set()
        self.scopes: dict[str, set[str]] = {}
        self.subjects: dict[str, set[str]] = {}
        for declared in rules:
            found = raised[declared.name]
            scopes = {scope for finding in found for scope in finding_scopes(finding)}
            subjects = {finding.subject for finding in found if finding.subject}
            if declared.subsumes:
                self.kinds |= scopes
            for name in declared.subsumes:
                self.scopes.setdefault(name, set()).update(scopes)
                self.subjects.setdefault(name, set()).update(subjects)

    def silences(self, finding: Finding, declared: FindingRule, node: TopologyNode | None) -> bool:
        """Whether a higher-order claim already says this fact more usefully."""

        return (
            finding.subject in self.subjects.get(declared.name, set())
            or finding.scope in self.scopes.get(declared.name, set())
            or (node is not None and node.kind_key in self.kinds)
        )


def beneath_causes(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    """``findings`` with each one a standing finding ``follows`` from said
    beneath the cause it leads back to, in their order."""

    keyed = [(finding_key(finding), finding) for finding in findings]
    found: dict[str, Finding] = {}
    for key, finding in keyed:
        found.setdefault(key, finding)
    order = {key: index for index, key in enumerate(found)}
    beneath: dict[str, list[Finding]] = {}
    standing = []
    for key, finding in keyed:
        cause = _root(key, found, order)
        if cause == key:
            standing.append(finding)
        else:
            beneath.setdefault(cause, []).append(finding)
    return tuple(_with(finding, beneath.get(finding_key(finding), ())) for finding in standing)


def _root(key: str, found: Mapping[str, Finding], order: Mapping[str, int]) -> str:
    """The key of the finding ``key`` leads back to through each first
    standing cause; in a loop of causes, the loop's first finding in ``order``."""

    chain = [key]
    while (cause := next((each for each in found[chain[-1]].follows if each in found), None)) is not None:
        if cause in chain:
            return min(chain[chain.index(cause) :], key=order.__getitem__)
        chain.append(cause)
    return chain[-1]


def _with(cause: Finding, consequences: Iterable[Finding]) -> Finding:
    """The cause with what follows from it said as its evidence and kept as its consequences."""

    beneath = tuple(consequences)
    if not beneath:
        return cause
    return replace(
        cause,
        evidence=(*cause.evidence, *(fact for consequence in beneath for fact in said_beneath(consequence))),
        consequences=beneath,
    )


def said_beneath(consequence: Finding) -> tuple[Fact, Fact]:
    """A consequence as its cause's evidence says it: what it is, linked to
    the page that offers its own remedy, and why."""

    page = consequence.primary or consequence.subject_link
    return (CAUSES, EntityLink(consequence.title, page.url if page else "")), (WHY, consequence.explanation)
