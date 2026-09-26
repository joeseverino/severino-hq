"""What each host section believes needs a decision now.

One function per domain, each referenced by that domain's descriptor in
``application.domains``. Nothing here knows the set of domains (the registry
does) so a section is added or removed by editing its descriptor and its
function, and no third place has to be kept in step.

Every function returns ``Insight``: the same shape an extension emits, so the
composed queue does not care who produced an entry. Two properties of that
shape are doing real work:

- ``url`` travels with the item: a domain knows which of its own filters
  answers its own backlog.
- ``value`` is the count. A domain with nothing outstanding returns ``()``
  rather than a zero, so the queue contains only real work and its length is
  the number of areas needing attention.
"""

from __future__ import annotations

import re

from django.db.models import Count, Q
from django.urls import reverse

from assets.models import Asset
from contacts import inbox
from content.models import ContentItem
from expenses.models import Expense
from receipts.models import Receipt

from .entity_links import entity_link
from .estate import subject_link
from .findings import derive_findings
from .infrastructure import enabled_resources, resource_health
from .projection import read_once
from .security import cli_principal
from .services import service_catalog
from . import sections
from .topology import derive_topology
from .ui import Insight, counted
from .workflow_contracts import ActionLink

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
    key: str,
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
            key=key,
        ),
    )


def documentation() -> tuple[Insight, ...]:
    return _backlog(
        count=sections.documentation_reading()["needing_review"],
        key="docs-review",
        eyebrow="Docs",
        title="Docs need review",
        body="Past their review date.",
        action="Review docs",
        url=f"{reverse('docs_index:list')}?needs_review=1",
    )


def content() -> tuple[Insight, ...]:
    return (
        *_backlog(
            count=sections.content_reading()["drafts"],
            key="content-drafts",
            eyebrow="Content",
            title="Draft content",
            body="Written but not published.",
            action="Open drafts",
            url=f"{reverse('content:list')}?status=draft",
        ),
        *_backlog(
            # Published, as the entry says. Counting drafts here meant every
            # new draft raised two entries: its own, and this one accusing it
            # of missing documentation it is far too early to have written.
            count=(
                ContentItem.objects.filter(status=ContentItem.Status.PUBLISHED)
                .annotate(doc_count=Count("related_documentation"))
                .filter(doc_count=0)
                .count()
            ),
            key="content-undocumented",
            eyebrow="Content",
            title="Content needs docs",
            body="Published with no linked documentation.",
            action="Link docs",
            url=f"{reverse('content:list')}?no_docs=1",
        ),
    )


def contacts_state() -> tuple[int, str]:
    """Unread submissions and whether D1 was reachable, as last read. No network:
    the snapshot and the queue both ask here, so they cannot disagree."""

    return read_once(CONTACTS_STATE_KEY, inbox.unread)


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
        eyebrow="Contacts",
        title="Unread contact submissions",
        body="Sent through the contact form and not yet answered.",
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
        key="expenses-without-receipts",
        eyebrow="Expenses",
        title="Expenses need receipts",
        body="No receipt attached.",
        action="Attach receipts",
        url=f"{reverse('expenses:list')}?no_receipts=1",
    )


def receipts() -> tuple[Insight, ...]:
    return _backlog(
        count=Receipt.objects.filter(
            related_expense__isnull=True, related_asset__isnull=True
        ).count(),
        key="receipts-unlinked",
        eyebrow="Receipts",
        title="Receipts need links",
        body="Not attached to an expense or an asset.",
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
        key="assets-missing-purchase",
        eyebrow="Assets",
        title="Assets missing purchase info",
        body="No purchase date or cost, so depreciation cannot be calculated.",
        action="Complete assets",
        url=f"{reverse('assets:list')}?missing_purchase=1",
    )


# A node key lasts 180 days here, so a warning at 75 would sit in the queue for
# more of the cycle than not, and a queue that is always non-empty is one nobody
# reads. Forty-five days is several unhurried weekends; fourteen is the point at
# which it stops being a plan and starts being a date.
KEY_EXPIRY_ATTENTION_DAYS = 45
KEY_EXPIRY_SERIOUS_DAYS = 14


_VERSION = re.compile(r"\d+(?:\.\d+)*")


def _version(text: str) -> tuple[int, ...]:
    """The numeric release a client reports, "1.102.3-t0abc" as (1, 102, 3)."""

    found = _VERSION.match(str(text or "").strip())
    return tuple(int(part) for part in found.group().split(".")) if found else ()


def _newest_client(presences) -> str:
    """The newest client release any device reports, as it reports it."""

    reported = [
        (_version(presence.client_version), presence.client_version)
        for _name, presence in presences
        if _version(presence.client_version)
    ]
    newest = max(reported, default=((), ""))
    return _VERSION.match(newest[1]).group() if newest[1] else ""


def _update_body(presence, newest: str) -> str:
    if not presence.client_version:
        return "Current version not reported."
    current = _VERSION.match(presence.client_version)
    running = current.group() if current else presence.client_version
    if newest and _version(newest) > _version(running):
        return f"{running} → {newest}."
    return f"Runs {running}."


def tailnet() -> tuple[Insight, ...]:
    """Two tailnet facts with no symptom until somebody needs them.

    A node key runs out on a date decided months earlier: the machine keeps
    running, keeps serving, and simply stops being reachable. Devices with
    expiry disabled are silent here, because for them there is no date.

    And a route that was advertised but never approved. `--advertise-routes`
    succeeds, the machine reports the route for as long as it runs, and the
    coordination server hands it to nobody, so a subnet route or an exit node
    can be configured, documented, believed, and dead, with every side of it
    reporting success.
    """

    # The presence table, not the whole machine catalogue. Everything shown
    # here is on the tailnet reading itself, and assembling every machine to
    # reach it costs the dashboard a query per row for facts it does not use.
    from .machines import tailnet_presence

    # Read once, asked twice. The projection has a query budget and this is
    # the same table both questions are about.
    presences = sorted(tailnet_presence().items())
    newest = _newest_client(presences)

    items = []
    for name, presence in presences:
        days = presence.key_expiry_days
        if days is None or days > KEY_EXPIRY_ATTENTION_DAYS:
            continue
        items.append(
            Insight(
                status="serious" if days <= KEY_EXPIRY_SERIOUS_DAYS else "attention",
                eyebrow="Tailnet",
                key=f"tailnet-expiry:{name}",
                title=(
                    f"{name} leaves the tailnet in {days} days"
                    if days > 0
                    else f"{name} has left the tailnet"
                ),
                value=str(max(days, 0)),
                body=(
                    "Its node key expires. Re-authenticate it or turn off key "
                    "expiry."
                    if days > 0
                    else "Its node key expired. Re-authenticate it."
                ),
                action="Open machine",
                url=entity_link("machine", name).url,
                subject=subject_link("machine", name),
            )
        )
    # Tailnet lock, which is a fact about the tailnet rather than about any one
    # machine's presence: a locked-out node is filtered out of the reading the
    # loop below walks, so asking each machine would never find one.
    from .tailnet import policy as tailnet_policy

    for name in tailnet_policy().locked_out:
        items.append(
            Insight(
                status="serious",
                eyebrow="Tailnet",
                key=f"tailnet-locked-out:{name}",
                title=f"{name} is locked out of the tailnet",
                value="1",
                body=(
                    "Tailnet lock is on and its key is unsigned, so other nodes "
                    "ignore it. Its own status still shows healthy."
                ),
                action="Sign it from a signing node",
                url=reverse("control_plane:tailnet"),
                subject=subject_link("machine", name),
            )
        )
    for name, presence in presences:
        if not presence.authorized:
            items.append(
                Insight(
                    status="serious",
                    eyebrow="Tailnet",
                    key=f"tailnet-unauthorized:{name}",
                    title=f"{name} is waiting for tailnet approval",
                    value="1",
                    body="It cannot reach anything until it is approved.",
                    action="Open machine",
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                )
            )
        if presence.lock_error:
            items.append(
                Insight(
                    status="serious",
                    eyebrow="Tailnet",
                    key=f"tailnet-lock-unsigned:{name}",
                    title=f"{name} is not signed for tailnet lock",
                    value="1",
                    body=(
                        f"{presence.lock_error} Other nodes ignore an "
                        "unsigned node."
                    ),
                    action="Open machine",
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                )
            )
        if presence.update_available:
            items.append(
                Insight(
                    status="attention",
                    eyebrow="Tailnet",
                    key=f"tailnet-update:{name}",
                    title=f"Tailscale update available for {name}",
                    value="1",
                    body=_update_body(presence, newest),
                    action="Open machine",
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                )
            )
        unapproved = presence.unapproved_routes
        if not unapproved:
            continue
        items.append(
            Insight(
                status="attention",
                eyebrow="Tailnet",
                key=f"tailnet-routes:{name}",
                title=(
                    f"{name} advertises "
                    f"{counted(len(unapproved), 'unapproved route', 'unapproved routes')}"
                ),
                value=str(len(unapproved)),
                body=(
                    f"Not approved: {', '.join(unapproved)}. "
                    + (
                        "It cannot be used as an exit node. "
                        # The swept fact, not a second reading of the route
                        # list: what makes a route an exit route is Tailscale's
                        # to say, and the sweep already asked.
                        if presence.offers_exit_node
                        and not presence.exit_node_approved
                        else ""
                    )
                    + "Approve the ones you want and stop advertising the rest."
                ),
                # The machine page, which offers the approval as a POST. A
                # queue entry links somewhere you can look before you act; the
                # verb lives where the routes it approves are shown.
                action="Approve routes",
                url=entity_link("machine", name).url,
                subject=subject_link("machine", name),
            )
        )
    return tuple(items)


# A neutral finding is context, which the queue leaves out.
_FINDING_STATUS = {"serious": "serious", "attention": "attention", "neutral": "neutral"}


def _node_link(node) -> ActionLink | None:
    """The page of a finding's subject, as the topology names it."""

    if node is None or not node.url:
        return None
    return ActionLink("subject", node.label, "read", node.url)


def infrastructure() -> tuple[Insight, ...]:
    """What infrastructure needs looking at: unsettled state, and deadlines.

    One entry per finding, and one per resource whose reconciled state is not
    settled that no finding already speaks for.

    Per resource rather than a single count: each one links to its own detail
    page, and "three resources need attention" is not actionable without
    knowing which. Severity distinguishes an outright failure from a state HQ
    simply cannot vouch for yet.
    """

    principal = cli_principal()
    topology = derive_topology(principal=principal)
    resources = enabled_resources()
    health_by_key = {resource.key: resource_health(resource) for resource in resources}
    actionable_keys = {
        resource.key
        for resource in resources
        if health_by_key[resource.key]["state"] not in {"pending", "declared"}
    }
    actionable_kinds = {
        resource.kind for resource in resources if resource.key in actionable_keys
    }
    # A resource subject waits until HQ has confirmed it; any other subject (a
    # connection, a domain, a machine) is a fact already observed.
    findings = tuple(
        finding
        for finding in derive_findings(topology, principal=principal)
        if (
            finding.subject.removeprefix("resource:") in actionable_keys
            if finding.subject.startswith("resource:")
            else bool(finding.subject) or not finding.scope or finding.scope in actionable_kinds
        )
    )
    nodes = {node.id: node for node in topology.nodes}
    # One item per finding, so each can be read, acted on and resolved on its
    # own. Grouping is the page's job, not the queue's.
    findings_url = reverse("control_plane:findings")
    items = [
        Insight(
            status=_FINDING_STATUS.get(finding.severity, "attention"),
            eyebrow="Finding",
            key=f"finding:{finding.rule}:{finding.subject or finding.scope}",
            title=finding.title,
            value="",
            body=finding.explanation,
            action="Review evidence",
            url=f"{findings_url}?rule={finding.rule}",
            subject=_node_link(nodes.get(finding.subject)),
        )
        for finding in findings
    ]
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
                key=f"resource:{resource.key}",
                title=(
                    f"{resource.key}: {health['message']}"
                    if health["message"]
                    else f"{resource.key} state is {health['state']}"
                ),
                value="1",
                body=(
                    "It reports a failure. Open it to see why."
                    if health["state"] == "degraded"
                    else "HQ cannot confirm its current state."
                ),
                action="Open resource",
                url=entity_link("resource", resource.key).url,
                subject=subject_link("resource", resource.key),
            )
        )
    return tuple(items) + tailnet()


def waiting_for_approval() -> tuple[Insight, ...]:
    """One item per change an agent asked for. Nothing is written until a person decides."""

    from .action_links import ActionLink
    from .approvals import pending, preview
    from .capabilities import capability_title

    items = []
    for held in pending():
        shown = preview(held)
        what = ", ".join(f"{row.path}: {row.after or '(empty)'}" for row in shown.rows[:3])
        items.append(
            Insight(
                status="serious",
                eyebrow="Approval",
                key=f"approval:{held.id}",
                title=(
                    f"{held.requested_actor} wants to run {capability_title(held.capability)}"
                    + (f" on {held.target}" if held.target else "")
                ),
                value="1",
                body=f"{shown.label}{f' · {what}' if what else ''}",
                url=reverse("core:approval_entry", kwargs={"approval_id": held.id}),
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
            key=f"service:{service.hostname}",
            title=f"{service.hostname} is not fully wired",
            value=str(len(service.faults)),
            body=" ".join(service.faults),
            action="Open service",
            url=service.url,
            subject=subject_link("service", service.hostname),
        )
        for service in service_catalog()
        if service.faults
    )
