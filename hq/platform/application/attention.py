"""What the host's sections raise on the queue: the findings of their rules, and the held requests.

A section raises nothing of its own: an item is a finding of a rule about its
records (``FindingRule.domain``), or a change an agent asked for that waits on
a person. Each is the ``Insight`` shape an extension emits.
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured

from hq.platform.application.routes import reverse

from .card_items import Insight
from .finding_model import Finding, FindingRule
from .findings import estate_findings, finding_key, finding_rules, rule_for
from .infrastructure import ResourceState, enabled_resources, resource_health
from .item_help import finding_plan
from .labels import lower_first
from .projection import read_once
from .security import cli_principal, internal_principal
from .tone import Tone
from .ui import counted, ended
lazy from .approval_review import asked, preview
lazy from .approvals import Offer, offers, pending, reads_every_hold
lazy from .domains import host_domains

# The section that raises a finding about the estate, which its subject gates.
INFRASTRUCTURE = "hq.infrastructure"
# The section that raises each change an agent asked for, held for a person.
APPROVALS = "hq.approvals"
# How a finding's severity reads before the bar its rule declares for it.
_RATED = {Tone.SERIOUS: "urgent once", Tone.ATTENTION: "worth attention once", Tone.NEUTRAL: "a note when"}
# The evidence a why quotes before the bar; the panel's facts hold the rest.
_WHY_EVIDENCE = 2


def finding_why(finding: Finding, rule: FindingRule | None) -> str:
    """What was read, then the bar it crossed: "Free on /: 1 GB of 30 GB (3.3%);
    urgent once free space is under 512 MB and 3% of the disk." The one place a
    finding's reason is worded, from its evidence and its rule's ``bars``."""

    quoted = finding.evidence[:_WHY_EVIDENCE]
    read = "; ".join(f"{label}: {value}" for label, value in quoted) or finding.title
    more = len(finding.evidence) - len(quoted)
    if more:
        read += f" (and {more} more)"
    bar = rule.bars.get(finding.severity, "") if rule is not None else ""
    return ended(f"{read}; {_RATED.get(finding.severity, _RATED[Tone.ATTENTION])} {bar}" if bar else read)


def raising_domains() -> frozenset[str]:
    """Each host section that raises items on the queue: Infrastructure for
    the estate's findings, each one a rule names for its records, and
    Approvals for the held requests."""

    named = frozenset({INFRASTRUCTURE, APPROVALS, *(rule.domain for rule in finding_rules() if rule.domain)})
    if unknown := named - {domain.id for domain in host_domains()}:
        raise ImproperlyConfigured(f"A finding rule names a section HQ does not have: {', '.join(sorted(unknown))}.")
    return named


def raised(domain: str) -> tuple[Insight, ...]:
    """What ``domain`` raises on the queue: the findings of each rule about
    its records, and under Infrastructure, each finding about the estate that
    HQ can speak for now."""

    return read_once("attention.raised", _raised).get(domain, ())


def infrastructure() -> tuple[Insight, ...]:
    """What Infrastructure raises: ``raised`` for the section of the estate's findings."""

    return raised(INFRASTRUCTURE)


def _raised() -> dict[str, tuple[Insight, ...]]:
    resources = enabled_resources()
    health_by_key = {resource.key: resource_health(resource) for resource in resources}
    findings_url = reverse("control_plane:findings")
    found: dict[str, list[Insight]] = {}
    for finding in _findings_to_raise(resources, health_by_key):
        rule = rule_for(finding.rule)
        section = (rule.domain if rule is not None else "") or INFRASTRUCTURE
        found.setdefault(section, []).append(finding_insight(finding, findings_url))
    return {domain: tuple(items) for domain, items in found.items()}


def _findings_to_raise(resources, health_by_key: dict[str, dict[str, str]]) -> tuple[Any, ...]:
    """The estate's findings whose subject HQ can speak for now."""

    actionable_keys = {
        resource.key
        for resource in resources
        if health_by_key[resource.key]["state"] not in {ResourceState.PENDING, ResourceState.DECLARED}
    }
    actionable_kinds = {resource.kind for resource in resources if resource.key in actionable_keys}
    return tuple(
        finding
        for finding in estate_findings(principal=cli_principal())
        if _subject_confirmed(finding, actionable_keys, actionable_kinds)
    )


def _subject_confirmed(finding: Any, actionable_keys: set[str], actionable_kinds: set[str]) -> bool:
    """Whether HQ has confirmed what a finding is about.

    A resource subject waits until HQ has confirmed it; any other subject (a
    connection, a domain, a machine) is a fact already observed. A finding
    with no subject that names a kind waits until one of that kind is
    confirmed.
    """

    if finding.subject.startswith("resource:"):
        return finding.subject.removeprefix("resource:") in actionable_keys
    return bool(finding.subject) or not finding.scope or finding.scope in actionable_kinds


def finding_insight(finding: Any, findings_url: str) -> Insight:
    """A finding as the queue's card carries it, wherever it is drawn: what
    was read that shows it, its plan, and the pages that help look into it."""

    rule = rule_for(finding.rule)
    return Insight(
        status=finding.severity,
        eyebrow="Finding",
        # A rule's findings are one kind of matter, named as the rule names itself.
        family=rule.title if rule is not None else "",
        why=finding_why(finding, rule),
        clears=rule.clears if rule is not None else "",
        key=finding_key(finding),
        title=finding.title,
        value=str(finding.count) if finding.count else "",
        magnitude=finding.count,
        notice=finding.notice,
        awaiting_fix=finding.awaiting_fix,
        body=finding.explanation,
        action=finding.primary.label if finding.primary else "",
        url=finding.primary.url if finding.primary else f"{findings_url}?rule={finding.rule}",
        subject=finding.subject_link,
        since=finding.since,
        observed_at=finding.observed_at,
        reads=finding.reads,
        workflow=finding_plan(finding, finding_key(finding)),
        facts=finding.evidence,
        links=_dedupe_links((*finding.investigations, *finding.offers)),
        read_capabilities=finding.read_capabilities,
        part=finding.part,
    )


def _dedupe_links(links: Iterable[Any]) -> tuple[Any, ...]:
    """Each page once, by its label."""

    seen: dict[str, Any] = {}
    for link in links:
        seen.setdefault(link.label, link)
    return tuple(seen.values())


def _approval_entry(held) -> str:
    return reverse("core:approval_entry", kwargs={"approval_id": held.id})


def approval_queue() -> str:
    """The audit log narrowed to the changes still waiting for a person."""

    return f"{reverse('core:audit_list')}?awaiting=1"


def waiting_for_approval() -> tuple[Insight, ...]:
    """One item per change an agent asked for. Nothing is written until a person decides."""

    items = []
    for held in pending(principal=internal_principal("action-queue")):
        shown = preview(held)
        sentence = asked(held, shown)
        items.append(
            Insight(
                status=Tone.ATTENTION,
                eyebrow="Approval",
                key=f"approval:{held.id}",
                title=str(sentence),
                value="1",
                body="" if sentence.whole else _asked_change(shown),
                why="An agent asked for this change, and HQ writes nothing until a person approves or rejects it.",
                url=_approval_entry(held),
                since=held.created_at,
                held=True,
            )
        )
    return tuple(items)


def readable_by[Item: Mapping[str, Any]](principal, items: list[Item]) -> list[Item]:
    """The composed queue as ``principal`` may read it.

    The queue is composed once for everybody at the operator's authority, so
    each door it leaves through to one reader takes out here what that reader
    may not be told: an item about what only some capabilities may read
    (``read_capabilities``), and, for a reader that sees only its own held
    requests (``approvals.reads_every_hold``), every other caller's. A held
    request is composed with no decision on it, and gains here what
    ``approvals.offers`` offers this reader.
    """

    items = [item for item in items if principal.permits(*item.get("read_capabilities", ()))]
    if not any(item.get("held") for item in items):
        return items
    held = {_approval_entry(row): row for row in pending(principal=internal_principal("action-queue"), limit=None)}
    if not reads_every_hold(principal):
        own = {row.pk for row in pending(principal=principal, limit=None)}
        items = [item for item in items if item.get("url") not in held or held[item["url"]].pk in own]
    offered = offers(principal, (held[item["url"]] for item in items if item.get("url") in held))
    return [_offering(item, offered[held[item["url"]].pk]) if item.get("url") in held else item for item in items]


def _offering[Item: Mapping[str, Any]](item: Item, offer: Offer) -> Item:
    """A held request's item with the decisions its reader may take, and why any other is withheld."""

    facts = [*item.get("facts", ()), *([["Decision", offer.withheld]] if offer.withheld else [])]
    return cast(Item, {**item, "actions": [*item.get("actions", ()), *map(asdict, offer.actions)], "facts": facts})


def _asked_change(shown) -> str:
    """What an asked-for change would do, as far as its first three fields."""

    moves = [
        (
            f"{lower_first(row.label)} from {row.before} to {row.after or 'empty'}"
            if row.before
            else f"{lower_first(row.label)} set to {row.after or 'empty'}"
        )
        for row in shown.changes[:3]
    ]
    if not moves:
        return ended(shown.label)
    more = len(shown.changes) - len(moves)
    return (
        f"{shown.label}: {', '.join(moves)}"
        + (f", and {counted(more, 'more field', 'more fields')}" if more > 0 else "")
        + "."
    )
