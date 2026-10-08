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

import re

from django.db.models import BooleanField, Count, ExpressionWrapper, Q
from hq.platform.application.routes import reverse

from hq.domains.assets.models import Asset
from hq.domains.contacts import inbox
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt

from .conditions import held_since
from .delivery_progress import STALLED_AFTER, WAITING, delivery_progress
from .entity_links import EntityLink, entity_link, kind_label
from .estate import subject_link
from .findings import estate_findings, finding_key, rule_for
from .first_seen import dated
from .infrastructure import enabled_resources, resource_health
from .item_help import commands, finding_plan, instructions, remedy_link
from .moments import ago, when_day
from .projection import read_once
from .references import dangling
from .security import cli_principal
from .services import service_catalog
from . import sections
from .topology import derive_topology
from .timestamps import moment
from .ui import Insight, counted, ended
from .workflow_contracts import ActionLink, WorkflowPlan

# Reconciliation states that mean the declared world and the real one disagree.
# "degraded" is a failure; the others are a resource HQ cannot currently vouch
# for, which is its own kind of thing to look at.
# A resource HQ has already asked the controller about is not something to ask
# an operator about. Pending clears itself on the next pass; degraded and
# unknown do not.
UNSETTLED_RESOURCE_STATES = frozenset({"degraded", "unknown"})
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
    names = ", ".join(link.label for link in first) + (
        f" and {counted(more, 'other')}" if first and more > 0 else ""
    )
    body = " ".join(part for part in (f"{names}." if names else "", body) if part)
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
            actions=tuple(
                ActionLink("open", link.label, "read", link.url) for link in first if link.url
            ),
        ),
    )


@dated
def documentation() -> tuple[Insight, ...]:
    return _backlog(
        count=sections.documentation_reading()["needing_review"],
        key="docs-review",
        eyebrow="Docs",
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
            eyebrow="Content",
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
            eyebrow="Content",
            one="published piece has no doc linked",
            many="published pieces have no doc linked",
            action="Link a doc",
            url=f"{reverse('content:list')}?no_docs=1",
        ),
    )


def contacts_state() -> tuple[int, str]:
    """Unread submissions and whether D1 was reachable, as last read. No network:
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
        eyebrow="Contacts",
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
        eyebrow="Expenses",
        one="expense has no receipt",
        many="expenses have no receipt",
        action="Attach receipts",
        url=f"{reverse('expenses:list')}?no_receipts=1",
    )
    if not counts["linked"]:
        return backlog
    return (*backlog, *dangling(Expense, principal=cli_principal(), source=lambda expense: expense.label))


@dated
def receipts() -> tuple[Insight, ...]:
    return _backlog(
        count=Receipt.objects.filter(
            related_expense__isnull=True, related_asset__isnull=True
        ).count(),
        key="receipts-unlinked",
        eyebrow="Receipts",
        one="receipt is not attached to an expense or asset",
        many="receipts are not attached to an expense or asset",
        action="Attach them",
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


def assets_missing_purchase() -> tuple[EntityLink, ...]:
    """Each active asset with no purchase date or no cost, by name."""

    return _asset_backlog()[0]


@dated
def assets() -> tuple[Insight, ...]:
    missing, linked = _asset_backlog()
    backlog = _backlog(
        count=len(missing),
        named=missing,
        key="assets-missing-purchase",
        eyebrow="Assets",
        one="asset has no purchase date or cost",
        many="assets have no purchase date or cost",
        body="Depreciation cannot be calculated without both.",
        action="Fill them in",
        url=f"{reverse('assets:list')}?missing_purchase=1",
    )
    if not linked:
        return backlog
    return (*backlog, *dangling(Asset, principal=cli_principal()))


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
    from .tailnet_presence import tailnet_presence

    # Read once, asked twice. The projection has a query budget and this is
    # the same table both questions are about.
    presences = sorted(tailnet_presence().items())
    newest = _newest_client(presences)

    items = []
    for name, presence in presences:
        days = presence.key_expiry_days
        if days is None or days > KEY_EXPIRY_ATTENTION_DAYS:
            continue
        expires = moment(presence.key_expires)
        on = f" on {when_day(expires)}" if expires else ""
        key = f"tailnet-expiry:{name}"
        items.append(
            Insight(
                status="serious" if days <= KEY_EXPIRY_SERIOUS_DAYS else "attention",
                eyebrow="Tailnet",
                family="Tailscale keys expiring",
                key=key,
                title=(
                    f"{name} leaves the tailnet in {counted(days, 'day')}"
                    if days > 0
                    else f"{name} has left the tailnet"
                ),
                value=str(max(days, 0)),
                body=(
                    f"Its Tailscale key expires{on}."
                    if days > 0
                    else f"Its Tailscale key expired{on}."
                ),
                url=entity_link("machine", name).url,
                subject=subject_link("machine", name),
                since=expires if days <= 0 else None,
                workflow=instructions(
                    key,
                    (
                        f"Sign in to Tailscale again on {name}, or turn off key expiry "
                        "for it in the Tailscale admin console."
                        if days > 0
                        else f"Sign in to Tailscale again on {name}."
                    ),
                ),
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
                family="Tailnet lock",
                key=f"tailnet-locked-out:{name}",
                title=f"{name} is locked out of the tailnet",
                value="1",
                body=(
                    f"Other machines ignore {name} because its key is not signed. "
                    "Its own status still shows healthy."
                ),
                url=reverse("control_plane:tailnet"),
                subject=subject_link("machine", name),
                workflow=_sign_key(f"tailnet-locked-out:{name}", name),
            )
        )
    for name, presence in presences:
        if not presence.authorized:
            items.append(
                Insight(
                    status="serious",
                    eyebrow="Tailnet",
                    family="Devices waiting for approval",
                    key=f"tailnet-unauthorized:{name}",
                    title=f"{name} is waiting for tailnet approval",
                    value="1",
                    body="It cannot reach anything yet.",
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                    workflow=instructions(
                        f"tailnet-unauthorized:{name}",
                        "Approve it in the Tailscale admin console, under Machines.",
                    ),
                )
            )
        if presence.lock_error:
            items.append(
                Insight(
                    status="serious",
                    eyebrow="Tailnet",
                    family="Tailnet lock",
                    key=f"tailnet-lock-unsigned:{name}",
                    title=f"{name} is not signed for tailnet lock",
                    value="1",
                    body=(
                        f"Other machines ignore {name} because its key is not signed. "
                        f"Tailscale says: {ended(presence.lock_error)}"
                    ),
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                    workflow=_sign_key(f"tailnet-lock-unsigned:{name}", name),
                )
            )
        if presence.update_available:
            items.append(
                Insight(
                    status="attention",
                    eyebrow="Tailnet",
                    family="Tailscale updates",
                    key=f"tailnet-update:{name}",
                    title=f"Tailscale update available for {name}",
                    value="1",
                    body=_update_body(presence, newest),
                    url=entity_link("machine", name).url,
                    subject=subject_link("machine", name),
                    workflow=commands(
                        f"tailnet-update:{name}",
                        ((f"On {name}, as an administrator", "tailscale update"),),
                    ),
                )
            )
        unapproved = presence.unapproved_routes
        if not unapproved:
            continue
        items.append(
            Insight(
                status="attention",
                eyebrow="Tailnet",
                family="Tailscale routes",
                key=f"tailnet-routes:{name}",
                title=(
                    f"{name} offers "
                    + counted(
                        len(unapproved),
                        "route that is not approved yet",
                        "routes that are not approved yet",
                    )
                ),
                value=str(len(unapproved)),
                body=(
                    f"Not approved: {', '.join(unapproved)}. "
                    + (
                        "It cannot be used as an exit node until its exit route is approved. "
                        # The swept fact, not a second reading of the route
                        # list: what makes a route an exit route is Tailscale's
                        # to say, and the sweep already asked.
                        if presence.offers_exit_node
                        and not presence.exit_node_approved
                        else ""
                    )
                    + "Approve the ones you want and stop offering the rest."
                ),
                url=entity_link("machine", name).url,
                subject=subject_link("machine", name),
                actions=_approve_routes(name),
            )
        )
    return tuple(items)


def _sign_key(key: str, name: str) -> WorkflowPlan:
    """Signing takes a tailnet lock key, which only a signing machine holds."""

    return commands(
        key,
        (("On a machine that can sign, list the keys waiting", "tailscale lock status"),),
        then=f"Then run tailscale lock sign with the key it lists for {name}.",
    )


def _approve_routes(name: str) -> tuple[ActionLink, ...]:
    """The route approval the machine's own page offers, as a remedy."""

    link = remedy_link(
        "tailnet.routes.approve", "Approve routes", name, url=entity_link("machine", name).url
    )
    return (link,) if link else ()


# A neutral finding is context, which the queue leaves out.
_FINDING_STATUS = {"serious": "serious", "attention": "attention", "neutral": "neutral"}


def _node_link(node) -> ActionLink | None:
    """The page of a finding's subject, as the topology names it."""

    if node is None or not node.url:
        return None
    return ActionLink("subject", node.label, "read", node.url)


@dated
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
        for finding in estate_findings(principal=principal)
        if (
            finding.subject.removeprefix("resource:") in actionable_keys
            if finding.subject.startswith("resource:")
            else bool(finding.subject) or not finding.scope or finding.scope in actionable_kinds
        )
    )
    nodes = {node.id: node for node in topology.nodes}
    # One item per finding, so each can be read, acted on and resolved on its
    # own. Folding them is the page's job; each says which family it is of.
    findings_url = reverse("control_plane:findings")
    items = [
        Insight(
            status=_FINDING_STATUS.get(finding.severity, "attention"),
            eyebrow="Finding",
            # A rule's findings are one kind of matter, named as the rule
            # names itself.
            family=getattr(rule_for(finding.rule), "title", ""),
            key=finding_key(finding),
            title=finding.title,
            value="",
            body=finding.explanation,
            url=f"{findings_url}?rule={finding.rule}",
            subject=_node_link(nodes.get(finding.subject)),
            since=finding.since,
            workflow=finding_plan(finding, finding_key(finding)),
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
        health = health_by_key[resource.key]
        if health["state"] == "deploying":
            # Told whatever else is open about it: no finding says this.
            items.append(_deploying(resource, health))
            continue
        if resource.key in covered_resources or resource.kind in covered_kinds:
            continue
        if health["state"] in UNSETTLED_RESOURCE_STATES:
            items.append(_unsettled(resource, health))
    return tuple(items) + tailnet()


def _unsettled(resource, health: dict[str, str]) -> Insight:
    """A record that reports a problem, or that could not be checked, and that
    no finding already speaks for."""

    named = f"{kind_label(resource.kind)} {resource.key}"
    failed = health["state"] == "degraded"
    if failed:
        body = ended(health["message"]) or "It gave no reason."
    elif resource.last_observed_at:
        body = f"It was last read {ago(resource.last_observed_at)}."
    else:
        body = "It has never been read."
    return Insight(
        status="serious" if failed else "attention",
        eyebrow="Infrastructure",
        key=f"resource:{resource.key}",
        title=f"{named} has a problem" if failed else f"{named} could not be checked",
        value="1",
        body=body,
        url=entity_link("resource", resource.key).url,
        subject=subject_link("resource", resource.key),
        since=held_since(resource.conditions, "Degraded") if failed else None,
        **_apply_help(resource),
    )


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
            action="Approve on GitHub" if progress.run_url else "Open the deploy",
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
        action="Open the deploy",
        url=link.url,
        subject=subject_link("resource", resource.key),
        notice=True,
    )


def _apply_help(resource) -> dict:
    """Apply HQ's settings again, where its type allows; otherwise say where to change it."""

    from hq.domains.control_plane.providers import PROVIDERS

    provider = PROVIDERS.get(resource.kind)
    policy = provider.actions.get("reconcile") if provider else None
    link = None
    if policy is None or policy.mode != "locked":
        link = remedy_link("infrastructure.reconcile", "Apply again", resource.key)
    if link is not None:
        return {"actions": (link,)}
    return {
        "workflow": instructions(
            f"resource:{resource.key}",
            "Change it where it lives. HQ is not allowed to change this type.",
        )
    }


def waiting_for_approval() -> tuple[Insight, ...]:
    """One item per change an agent asked for. Nothing is written until a person decides."""

    from .action_links import ActionLink
    from .approvals import pending, preview
    from .capabilities import capability_title

    items = []
    for held in pending():
        shown = preview(held)
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
                body=_asked_change(shown),
                url=reverse("core:approval_entry", kwargs={"approval_id": held.id}),
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


def _asked_change(shown) -> str:
    """What an asked-for change would do, as far as its first three fields."""

    moves = [
        (
            f"{row.path} from {row.before} to {row.after or 'empty'}"
            if row.before
            else f"{row.path} to {row.after or 'empty'}"
        )
        for row in shown.rows[:3]
    ]
    if not moves:
        return ended(shown.label)
    more = len(shown.rows) - len(moves)
    return (
        f"{shown.label}: {', '.join(moves)}"
        + (f", and {counted(more, 'more field', 'more fields')}" if more > 0 else "")
        + "."
    )


@dated
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
            title=f"{service.hostname} is not set up properly",
            value=str(len(service.faults)),
            body=" ".join(service.faults),
            action="Open the service",
            url=service.url,
            subject=subject_link("service", service.hostname),
        )
        for service in service_catalog()
        if service.faults
    )
