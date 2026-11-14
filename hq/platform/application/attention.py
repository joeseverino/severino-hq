"""What each host section believes needs a decision now.

One function per domain, referenced by its descriptor in ``application.domains``
(nothing here knows the set). Each returns ``Insight``, the shape an extension
emits, with the ``url`` of the domain's own filter for its backlog; one with
nothing outstanding returns ``()`` rather than a zero, so the queue holds only
real work and its length is the number of areas needing attention.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from django.db.models import BooleanField, Count, ExpressionWrapper, Q

from hq.domains.assets.models import Asset
from hq.domains.contacts import inbox
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.platform.application.routes import reverse

from . import sections
from .card_items import Insight
from .delivery_progress import STALLED_AFTER, WAITING, delivery_progress
from .entity_links import EntityLink, entity_link
from .estate import subject_link
from .findings import estate_findings, finding_key, rule_for
from .first_seen import dated
from .infrastructure import enabled_resources, resource_health
from .item_help import finding_plan
from .labels import lower_first
from .projection import read_once
from .references import dangling
from .security import cli_principal, internal_principal
from .ui import counted, ended
from .workflow_contracts import ActionLink
lazy from .approval_review import asked, preview
lazy from .approvals import pending, reads_every_hold

# How many of a backlog's things a card says by name before it counts the rest.
MOST_NAMED = 3
CONTACTS_STATE_KEY = "attention.contacts-state"


def _backlog(
    *,
    key: str,
    count: int,
    eyebrow: str,
    one: str,
    many: str,
    action: str,
    url: str,
    body: str = "",
    notice: bool = False,
    named: tuple[EntityLink, ...] = (),
) -> tuple[Insight, ...]:
    """One Insight when there is something to do, nothing when there is not.

    The title is the count and what is true of them, said whole for one and
    for many. ``action`` names the page the work is done on, which is the
    help every backlog comes with. ``named`` is the things themselves, where
    the domain read them: the first few are said by name, each a link to its
    own page, and the rest are a count."""

    if not count:
        return ()
    first = named[:MOST_NAMED]
    more = count - len(first)
    names = ", ".join(link.label for link in first) + (f" and {counted(more, 'other')}" if first and more > 0 else "")
    body = " ".join(part for part in (ended(names), body) if part)
    return (
        Insight(
            status="attention",
            eyebrow=eyebrow,
            title=counted(count, one, many),
            value=str(count),
            body=body,
            action=action,
            url=url,
            magnitude=count,
            key=key,
            notice=notice,
            actions=tuple(ActionLink("open", link.label, "read", link.url) for link in first if link.url),
        ),
    )


@dated
def documentation() -> tuple[Insight, ...]:
    return _backlog(
        count=sections.documentation_reading()["needing_review"],
        key="docs-review",
        eyebrow="Projects",
        one="doc is past its review date",
        many="docs are past their review date",
        action="Review docs",
        url=f"{reverse('docs_index:list')}?needs_review=1",
    )


@dated
def content() -> tuple[Insight, ...]:
    return (
        *_backlog(
            count=sections.content_reading()["drafts"],
            key="content-drafts",
            eyebrow="Web",
            one="draft is not published",
            many="drafts are not published",
            action="Open drafts",
            url=f"{reverse('content:list')}?status=draft",
            # A draft is work in hand, not a problem to fix.
            notice=True,
        ),
        *_backlog(
            # Published only: a draft already has its own entry and is too
            # early to be missing documentation.
            count=(
                ContentItem.objects.filter(status=ContentItem.Status.PUBLISHED)
                .annotate(doc_count=Count("related_documentation"))
                .filter(doc_count=0)
                .count()
            ),
            key="content-undocumented",
            eyebrow="Web",
            one="published piece has no doc linked",
            many="published pieces have no doc linked",
            action="Link a doc",
            url=f"{reverse('content:list')}?no_docs=1",
        ),
    )


def contacts_state() -> tuple[int, str]:
    """Unread submissions and whether the last read reached D1. No network:
    the snapshot and the queue both ask here, so they cannot disagree."""

    return read_once(CONTACTS_STATE_KEY, inbox.unread)


@dated
def contacts() -> tuple[Insight, ...]:
    """Unread submissions from the public site.

    An unreachable upstream reports nothing outstanding rather than raising:
    the dashboard composes every domain, and one bad upstream must not take the
    whole page down. The outage surfaces as upstream health instead, which is
    where it belongs.
    """

    count, _ = contacts_state()
    return _backlog(
        count=count,
        key="contacts-unread",
        eyebrow="Web",
        one="unread message from the contact form",
        many="unread messages from the contact form",
        action="Read messages",
        url=f"{reverse('contacts:list')}?status=unread",
    )


@dated
def expenses() -> tuple[Insight, ...]:
    # One statement says how many lack a receipt and whether any names
    # something, so a ledger with no links costs nothing more to check.
    counts = Expense.objects.aggregate(
        bare=Count("pk", filter=Q(receipts__isnull=True), distinct=True),
        linked=Count("pk", filter=~Q(paid_from="") | ~Q(about=""), distinct=True),
    )
    backlog = _backlog(
        count=counts["bare"],
        key="expenses-without-receipts",
        eyebrow="Business",
        one="expense has no receipt",
        many="expenses have no receipt",
        action="Attach receipts",
        url=f"{reverse('expenses:list')}?no_receipts=1",
    )
    if not counts["linked"]:
        return backlog
    return (*backlog, *dangling(Expense, principal=cli_principal()))


@dated
def receipts() -> tuple[Insight, ...]:
    return _backlog(
        count=Receipt.objects.filter(related_expense__isnull=True, related_asset__isnull=True).count(),
        key="receipts-unlinked",
        eyebrow="Business",
        one="receipt is not attached to an expense or asset",
        many="receipts are not attached to an expense or asset",
        action="Attach receipts",
        url=f"{reverse('receipts:list')}?unlinked=1",
    )


def _asset_backlog() -> tuple[tuple[EntityLink, ...], bool]:
    """Each active asset with no purchase date or no cost, by name, and
    whether any asset holds a link to check. One read."""

    unpriced = Q(status=Asset.Status.ACTIVE) & (Q(purchase_date__isnull=True) | Q(total_cost=0))
    rows = tuple(
        Asset.objects.filter(unpriced | ~Q(infrastructure=""))
        .annotate(unpriced=ExpressionWrapper(unpriced, output_field=BooleanField()))
        .order_by("item_name")
        .values_list("item_name", "slug", "unpriced", "infrastructure")
    )
    missing = tuple(entity_link("asset", slug, label=name) for name, slug, lacks, _linked in rows if lacks)
    return missing, any(linked for _name, _slug, _lacks, linked in rows)


@dated
def assets() -> tuple[Insight, ...]:
    missing, linked = _asset_backlog()
    backlog = _backlog(
        count=len(missing),
        named=missing,
        key="assets-missing-purchase",
        eyebrow="Business",
        one="asset has no purchase date or cost",
        many="assets have no purchase date or cost",
        body="Depreciation cannot be calculated without both.",
        action="Fill in details",
        url=f"{reverse('assets:list')}?missing_purchase=1",
    )
    if not linked:
        return backlog
    return (*backlog, *dangling(Asset, principal=cli_principal()))


# A neutral finding is context, which the queue leaves out.
_FINDING_STATUS = {"serious": "serious", "attention": "attention", "neutral": "neutral"}


@dated
def infrastructure() -> tuple[Insight, ...]:
    """What infrastructure needs looking at: one entry per finding HQ can speak
    for now, each read, acted on and resolved on its own, and one per deploy on
    its way to production, which is no finding (``delivery_progress``)."""

    resources = enabled_resources()
    health_by_key = {resource.key: resource_health(resource) for resource in resources}
    findings_url = reverse("control_plane:findings")
    return (
        *(finding_insight(finding, findings_url) for finding in _findings_to_raise(resources, health_by_key)),
        *(
            _deploying(resource, health)
            for resource in resources
            if (health := health_by_key[resource.key])["state"] == "deploying"
        ),
    )


def _findings_to_raise(resources, health_by_key: dict[str, dict[str, str]]) -> tuple[Any, ...]:
    """The estate's findings whose subject HQ can speak for now."""

    actionable_keys = {
        resource.key for resource in resources if health_by_key[resource.key]["state"] not in {"pending", "declared"}
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

    return Insight(
        status=_FINDING_STATUS.get(finding.severity, "attention"),
        eyebrow="Finding",
        # A rule's findings are one kind of matter, named as the rule names itself.
        family=rule.title if (rule := rule_for(finding.rule)) is not None else "",
        key=finding_key(finding),
        title=finding.title,
        value="",
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


def _deploying(resource, health: dict[str, str]) -> Insight:
    """A delivery on its way to production: a notice while a deploy runs or is
    about to, and a thing to do while a run waits for approval."""

    progress = delivery_progress(resource)
    link = entity_link("resource", resource.key)
    said = ended(health["message"])
    if progress.state == WAITING:
        return Insight(
            status="attention",
            eyebrow="Infrastructure",
            key=f"resource:{resource.key}",
            title="A deploy is waiting for your approval",
            value="1",
            body=said,
            action="Approve on GitHub" if progress.run_url else "Open deploy",
            url=progress.run_url or link.url,
            subject=subject_link("resource", resource.key),
            since=progress.since,
        )
    minutes = int(STALLED_AFTER.total_seconds() // 60)
    return Insight(
        status="attention",
        eyebrow="Infrastructure",
        key=f"resource:{resource.key}",
        title="A deploy is running" if progress.state == "running" else "A deploy is about to start",
        value="1",
        body=f"{said} It becomes a problem if production is still behind after {counted(minutes, 'minute')}.",
        action="Open deploy",
        url=link.url,
        subject=subject_link("resource", resource.key),
        notice=True,
    )


def _approval_entry(held) -> str:
    return reverse("core:approval_entry", kwargs={"approval_id": held.id})


def approval_queue() -> str:
    """The audit log narrowed to the changes still waiting for a person."""

    return f"{reverse('core:audit_list')}?awaiting=1"


# The start of a held request's key in the queue, after its source's id.
APPROVAL_KEY = "approval:"


def waiting_for_approval() -> tuple[Insight, ...]:
    """One item per change an agent asked for. Nothing is written until a person decides."""

    items = []
    for held in pending(principal=internal_principal("action-queue")):
        shown = preview(held)
        sentence = asked(held, shown)
        items.append(
            Insight(
                status="attention",
                eyebrow="Approval",
                key=f"{APPROVAL_KEY}{held.id}",
                title=str(sentence),
                value="1",
                body="" if sentence.whole else _asked_change(shown),
                url=_approval_entry(held),
                since=held.created_at,
                actions=tuple(
                    ActionLink(
                        name=f"approval.{decision}",
                        label=label,
                        effect="remote_write",
                        url=reverse(
                            "control_plane:approval_decide",
                            kwargs={"approval_id": held.id, "decision": decision},
                        ),
                        method="POST",
                        recommended=decision == "approve",
                    )
                    for decision, label in (("approve", "Approve"), ("reject", "Reject"))
                ),
            )
        )
    return tuple(items)


def readable_by[Item: Mapping[str, Any]](principal, items: list[Item]) -> list[Item]:
    """The composed queue as ``principal`` may read it.

    The queue is composed once for everybody at the operator's authority, so
    each door it leaves through to one reader takes out here what that reader
    may not be told: an item about what only some capabilities may read
    (``read_capabilities``), and, for a reader that sees only its own held
    requests (``approvals.reads_every_hold``), every other caller's.
    """

    items = [item for item in items if principal.permits(*item.get("read_capabilities", ()))]
    if reads_every_hold(principal):
        return items
    own = {held.pk for held in pending(principal=principal, limit=None)}
    hidden = {
        _approval_entry(held)
        for held in pending(principal=internal_principal("action-queue"), limit=None)
        if held.pk not in own
    }
    return [item for item in items if item.get("url") not in hidden]


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
