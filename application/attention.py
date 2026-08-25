"""What each host section believes needs a decision now.

One function per domain, each referenced by that domain's descriptor in
``application.domains``. Nothing here knows the set of domains -- the registry
does -- so a section is added or removed by editing its descriptor and its
function, and no third place has to be kept in step.

Every function returns ``Insight``: the same shape an extension emits, so the
composed queue does not care who produced an entry. Two properties of that
shape are doing real work:

- ``url`` travels with the item. The dashboard used to keep a code-to-URL table
  purely to rejoin a work-queue entry with the filtered list that shows it, and
  a code present in one and missing from the other was a ``KeyError`` on the
  home page. A domain knows which of its own filters answers its own backlog.
- ``value`` is the count. A domain with nothing outstanding returns ``()``
  rather than a zero, so the queue contains only real work and its length is
  the number of areas needing attention.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.urls import reverse

from assets.models import Asset
from contacts.d1 import D1Error, get_unread_count
from content.models import ContentItem
from control_plane.models import ManagedResource
from expenses.models import Expense
from receipts.models import Receipt

from .findings import derive_findings
from .infrastructure import resource_health
from .projection import read_once
from .security import cli_principal
from .services import service_catalog
from . import sections
from .topology import derive_topology
from .ui import Insight

# Reconciliation states that mean the declared world and the real one disagree.
# "degraded" is a failure; the others are a resource HQ cannot currently vouch
# for, which is its own kind of thing to look at.
# A resource HQ has already asked the controller about is not something to ask
# an operator about. Pending clears itself on the next pass; degraded and
# unknown do not.
UNSETTLED_RESOURCE_STATES = frozenset({"degraded", "unknown"})
CONTACTS_STATE_KEY = "attention.contacts-state"


def _backlog(
    *,
    count: int,
    eyebrow: str,
    title: str,
    body: str,
    action: str,
    url: str,
    status: str = "attention",
) -> tuple[Insight, ...]:
    """One Insight when there is something to do, nothing when there is not."""

    if not count:
        return ()
    return (
        Insight(
            status=status,
            eyebrow=eyebrow,
            title=title,
            value=str(count),
            body=body,
            action=action,
            url=url,
            magnitude=count,
        ),
    )


def documentation() -> tuple[Insight, ...]:
    return _backlog(
        count=sections.documentation_reading()["needing_review"],
        eyebrow="Docs",
        title="Docs need review",
        body="Documentation past its review interval, so it may no longer be true.",
        action="Review docs",
        url=f"{reverse('docs_index:list')}?needs_review=1",
    )


def content() -> tuple[Insight, ...]:
    return (
        *_backlog(
            count=sections.content_reading()["drafts"],
            eyebrow="Content",
            title="Draft content",
            body="Written but not published.",
            action="Open drafts",
            url=f"{reverse('content:list')}?status=draft",
        ),
        *_backlog(
            # Published, as the entry says. Counting drafts here meant every
            # new draft raised two entries -- its own, and this one accusing it
            # of missing documentation it is far too early to have written.
            count=(
                ContentItem.objects.filter(status=ContentItem.Status.PUBLISHED)
                .annotate(doc_count=Count("related_documentation"))
                .filter(doc_count=0)
                .count()
            ),
            eyebrow="Content",
            title="Content needs docs",
            body="Published work with no documentation record linking it back.",
            action="Link docs",
            url=f"{reverse('content:list')}?no_docs=1",
        ),
    )


def _contacts_state() -> tuple[int, str]:
    """Unread submissions and the upstream's health, from one D1 read.

    The single place HQ asks D1 how many submissions are waiting. The snapshot
    reports the upstream's health and the queue reports the backlog; both read
    it here, so the two can never disagree about whether contacts is reachable,
    and a test has one thing to patch.

    The public function below memoises only inside one projection scope. There
    is no process lifetime and therefore no stale value for a later request.
    """

    try:
        return get_unread_count(), "ok"
    except D1Error:
        return 0, "unavailable"


def contacts_state() -> tuple[int, str]:
    return read_once(CONTACTS_STATE_KEY, _contacts_state)


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
        eyebrow="Contacts",
        title="Unread contact submissions",
        body="Someone wrote in through jseverino.com and has not been answered.",
        action="Read submissions",
        url=f"{reverse('contacts:list')}?status=unread",
    )


def expenses() -> tuple[Insight, ...]:
    return _backlog(
        count=(
            Expense.objects.annotate(receipt_count=Count("receipts"))
            .filter(receipt_count=0)
            .count()
        ),
        eyebrow="Expenses",
        title="Expenses need receipts",
        body="Recorded spend with nothing filed to substantiate it.",
        action="Attach receipts",
        url=f"{reverse('expenses:list')}?no_receipts=1",
    )


def receipts() -> tuple[Insight, ...]:
    return _backlog(
        count=Receipt.objects.filter(
            related_expense__isnull=True, related_asset__isnull=True
        ).count(),
        eyebrow="Receipts",
        title="Receipts need links",
        body="Filed receipts not yet attached to an expense or an asset.",
        action="Link receipts",
        url=f"{reverse('receipts:list')}?unlinked=1",
    )


def assets() -> tuple[Insight, ...]:
    return _backlog(
        count=(
            Asset.objects.filter(status=Asset.Status.ACTIVE)
            .filter(Q(purchase_date__isnull=True) | Q(total_cost=0))
            .count()
        ),
        eyebrow="Assets",
        title="Assets missing purchase info",
        body="Active assets with no purchase date or cost, so they cannot be "
        "depreciated.",
        action="Complete assets",
        url=f"{reverse('assets:list')}?missing_purchase=1",
    )


# A node key lasts 180 days here, so a warning at 75 would sit in the queue for
# more of the cycle than not, and a queue that is always non-empty is one nobody
# reads. Forty-five days is several unhurried weekends; fourteen is the point at
# which it stops being a plan and starts being a date.
KEY_EXPIRY_ATTENTION_DAYS = 45
KEY_EXPIRY_SERIOUS_DAYS = 14


def tailnet() -> tuple[Insight, ...]:
    """Machines whose tailnet key runs out soon.

    A deadline nothing else in HQ watches, and one with no symptom until it
    passes: the machine keeps running, keeps serving, and simply stops being
    reachable over the tailnet on a date decided months earlier. Devices with
    expiry disabled are silent here, because for them there is no date.
    """

    # The presence table, not the whole machine catalogue. Everything shown
    # here is on the tailnet reading itself, and assembling every machine to
    # reach it costs the dashboard a query per row for facts it does not use.
    from .machines import tailnet_presence

    items = []
    for name, presence in sorted(tailnet_presence().items()):
        days = presence.key_expiry_days
        if days is None or days > KEY_EXPIRY_ATTENTION_DAYS:
            continue
        items.append(
            Insight(
                status="serious" if days <= KEY_EXPIRY_SERIOUS_DAYS else "attention",
                eyebrow="Tailnet",
                title=(
                    f"{name} leaves the tailnet in {days} days"
                    if days > 0
                    else f"{name} has left the tailnet"
                ),
                value=str(max(days, 0)),
                body=(
                    f"Its node key expires. {name} keeps running and stops "
                    "being reachable over the tailnet."
                ),
                action="Open machine",
                url=reverse("control_plane:machine", kwargs={"name": name}),
            )
        )
    return tuple(items)


def infrastructure() -> tuple[Insight, ...]:
    """What infrastructure needs looking at: unsettled state, and deadlines.

    One entry per resource whose reconciled state is not settled.

    Per resource rather than a single count: each one links to its own detail
    page, and "three resources need attention" is not actionable without
    knowing which. Severity distinguishes an outright failure from a state HQ
    simply cannot vouch for yet.
    """

    principal = cli_principal()
    topology = derive_topology(principal=principal)
    resources = tuple(ManagedResource.objects.filter(enabled=True))
    health_by_key = {resource.key: resource_health(resource) for resource in resources}
    actionable_keys = {
        resource.key
        for resource in resources
        if health_by_key[resource.key]["state"] not in {"pending", "declared"}
    }
    actionable_kinds = {
        resource.kind for resource in resources if resource.key in actionable_keys
    }
    findings = tuple(
        finding
        for finding in derive_findings(topology, principal=principal)
        if (
            finding.subject.removeprefix("resource:") in actionable_keys
            if finding.subject
            else not finding.scope or finding.scope in actionable_kinds
        )
    )
    items = (
        [
            Insight(
                status=(
                    "serious"
                    if any(finding.severity == "serious" for finding in findings)
                    else "attention"
                ),
                eyebrow="Finding",
                title="Infrastructure findings",
                value=str(len(findings)),
                body=(
                    f"{len(findings)} claim{'s' if len(findings) != 1 else ''} "
                    "derived from the live topology. Open the evidence to see "
                    "every affected subject or kind."
                ),
                action="Review evidence",
                url=reverse("control_plane:findings"),
                magnitude=len(findings),
            )
        ]
        if findings
        else []
    )
    covered_resources = {
        finding.subject.removeprefix("resource:")
        for finding in findings
        if finding.subject.startswith("resource:")
    }
    covered_kinds = {finding.scope for finding in findings if finding.scope}
    for resource in resources:
        if resource.key in covered_resources or resource.kind in covered_kinds:
            continue
        health = health_by_key[resource.key]
        if health["state"] not in UNSETTLED_RESOURCE_STATES:
            continue
        items.append(
            Insight(
                status="serious" if health["state"] == "degraded" else "attention",
                eyebrow="Infrastructure",
                title=(
                    f"{resource.key}: {health['message']}"
                    if health["message"]
                    else f"{resource.key} needs infrastructure attention"
                ),
                value="1",
                body=f"Declared state for {resource.key} is {health['state']}.",
                action="Open resource",
                url=reverse("control_plane:detail", kwargs={"key": resource.key}),
            )
        )
    return tuple(items) + tailnet()


def services() -> tuple[Insight, ...]:
    """One entry per hostname whose wiring is incomplete.

    Wiring only, and deliberately no overlap with ``infrastructure`` above.
    Whether a declared resource reconciled is reported there, per resource;
    saying it again here would put one problem in the queue twice under two
    names and make the count of things needing attention wrong.

    What is left is what no single resource can see: a name something answers
    for with no certificate covering it, an ingress pointing at a host HQ does
    not know, two declarations of the same kind contradicting each other.
    """

    return tuple(
        Insight(
            status="attention",
            eyebrow="Services",
            title=f"{service.hostname} is incompletely wired",
            value=str(len(service.faults)),
            body=" ".join(service.faults),
            action="Open service",
            url=service.url,
        )
        for service in service_catalog()
        if service.faults
    )
