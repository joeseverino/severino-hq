"""What the host's sections raise on the queue: the findings of their rules, and the held requests.

A section raises nothing of its own: an item is a finding of a rule about its
records (``FindingRule.domain``), or a change an agent asked for that waits on
a person. Each is a ``Finding``, as an extension's are, and reaches a page
worded by ``dashboard.finding_rows``.
"""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, cast

from django.core.exceptions import ImproperlyConfigured

from hq.platform.application.routes import reverse

from .finding_model import Finding, FindingRule, opens
from .findings import estate_findings, finding_rules, rule_for
from .infrastructure import ResourceState, enabled_resources, resource_health
from .labels import lower_first
from .projection import read_once
from .references import Kind
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

    if rule is None and finding.why:
        return finding.why
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


def raised(domain: str) -> tuple[Finding, ...]:
    """What ``domain`` raises on the queue: the findings of each rule about
    its records, and under Infrastructure, each finding about the estate that
    HQ can speak for now."""

    return read_once("attention.raised", _raised).get(domain, ())


def infrastructure() -> tuple[Finding, ...]:
    """What Infrastructure raises: ``raised`` for the section of the estate's findings."""

    return raised(INFRASTRUCTURE)


def _raised() -> dict[str, tuple[Finding, ...]]:
    resources = enabled_resources()
    health_by_key = {resource.key: resource_health(resource) for resource in resources}
    found: dict[str, list[Finding]] = {}
    for finding in _findings_to_raise(resources, health_by_key):
        rule = rule_for(finding.rule)
        section = (rule.domain if rule is not None else "") or INFRASTRUCTURE
        found.setdefault(section, []).append(finding)
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

    if key := Kind.RESOURCE.identity_in(finding.subject):
        return key in actionable_keys
    return bool(finding.subject) or not finding.scope or finding.scope in actionable_kinds


def _approval_entry(held) -> str:
    return reverse("core:approval_entry", kwargs={"approval_id": held.id})


def approval_queue() -> str:
    """The audit log narrowed to the changes still waiting for a person."""

    return f"{reverse('core:audit_list')}?awaiting=1"


# What a held request is raised under: no host rule, so it says its own why.
APPROVAL = "approval"


def waiting_for_approval() -> tuple[Finding, ...]:
    """One item per change an agent asked for. Nothing is written until a person decides."""

    items = []
    for held in pending(principal=internal_principal("action-queue")):
        shown = preview(held)
        sentence = asked(held, shown)
        items.append(
            Finding(
                rule=APPROVAL,
                subject=str(held.id),
                severity=Tone.ATTENTION,
                title=str(sentence),
                explanation="" if sentence.whole else _asked_change(shown),
                why="An agent asked for this change, and HQ writes nothing until an operator approves or rejects it.",
                primary=opens("", _approval_entry(held)),
                since=held.created_at,
                held=True,
            )
        )
    return tuple(items)


def readable_by[Item: Mapping[str, Any]](principal, items: list[Item]) -> list[Item]:
    """The composed queue as ``principal`` may read it.

    The queue is composed once at the operator's authority, so each door to one
    reader takes out here what that reader may not be told, and adds the
    decisions ``approvals.offers`` offers it.
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
            else f"{lower_first(row.label)} set to {row.after}"
        )
        for row in shown.changes[:3]
    ]
    more = len(shown.changes) - len(moves)
    rest = f", and {counted(more, 'more field', 'more fields')}" if more > 0 else ""
    return ended(f"{shown.label}: {', '.join(moves)}{rest}") if moves else ""
