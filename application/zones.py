"""A domain: one zone, and every record published in it.

The services view answers "does this name work". This answers a different
question that the same declarations already contain: "what does this domain
actually say". They are not the same question, and neither is a substitute for
the other: a DMARC policy, a CAA restriction and an MX record are not services
and never appear there, yet getting them wrong is how mail stops arriving and
how anyone in the world becomes able to obtain a certificate for the domain.

Nothing here is stored. A zone is derived from three things that already exist:
the domains an operator declared, the records declared inside them, and the last
controller sweep of what the provider actually holds. There is deliberately no
Zone model: a stored copy could disagree with the declarations, and being the
thing that cannot disagree is the entire value.

What the page says *about* a zone is contributed rather than listed here: see
``ZONE_INSIGHTS`` and ``zone_insights``. Those observations stay descriptions
rather than drift, because HQ holds a credential that can read and write DNS
records and nothing else. It cannot change a zone's TLS posture, so it does not
get to have an opinion about it: stating "DMARC is monitoring only" is true and
useful, and flagging it as drift would invent a policy the operator never
declared and that nothing here could enforce. The exceptions are the two things that are wrong by their own
definition: a challenge record that outlived its issuance, and a CAA record that
forbids the authority HQ renews with.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from django.db import transaction
from django.urls import reverse

from control_plane.models import ManagedResource, ProviderInventory
from control_plane.providers import (
    DNS_RECORD_KIND,
    DNS_RECORD_TYPES_BY_ID,
    ZONE_KIND,
    normalized_hostname,
)

from .entity_links import entity_link
from .infrastructure import resource_health
from .inventory import unmanaged
from .ui import ListRow

RECORD_KIND = DNS_RECORD_KIND


# Records HQ creates and deletes inside a single operation, rather than keeps
# true.
#
# There is one owner of everything in a declared domain, and it is HQ. The
# distinction is not who made a record but how long it is meant to last:
# desired state HQ holds to, or working material an operation makes and clears
# up after itself. An ACME challenge is the second. It exists for the seconds
# an authority takes to verify a request, and declaring one would mean HQ
# recreating it immediately after the issuance that made it was finished with
# it: HQ fighting itself.
#
# So these are never offered for adoption and never counted as outstanding
# work. They are also not listed among a zone's records, because a record that
# exists for seconds is not something an operator browses; the only interesting
# case is one that outlived its issuance, and that has an insight of its own.
EPHEMERAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("_acme-challenge.", "certificate issuance"),
)


def ephemeral_operation(name: str) -> str:
    """The HQ operation a record belongs to, when it is working material."""

    candidate = normalized_hostname(name)
    for prefix, operation in EPHEMERAL_PREFIXES:
        if candidate.startswith(prefix):
            return operation
    return ""


@dataclass(frozen=True)
class ZoneRecord:
    """One row of a zone, whether or not HQ declares it."""

    name: str
    record_type: str
    content: str
    priority: Any = None
    proxied: bool = False
    ttl: Any = 1
    # Set when HQ declares this record; blank when the provider merely holds it.
    resource_key: str = ""
    health: dict[str, str] | None = None
    # Set only when unmanaged, and the handle adoption uses.
    token: str = ""
    # Unmanaged and read through a connection that only observes.
    observed_only: bool = False

    @property
    def managed(self) -> bool:
        return bool(self.resource_key)

    @property
    def declares_service(self) -> bool:
        record_type = DNS_RECORD_TYPES_BY_ID.get(self.record_type)
        return bool(record_type and record_type.declares_service)

    @property
    def secondary(self) -> bool:
        """States policy or proves ownership rather than sending traffic."""

        record_type = DNS_RECORD_TYPES_BY_ID.get(self.record_type)
        return bool(record_type and record_type.secondary)

    @property
    def ephemeral(self) -> str:
        """The HQ operation this record belongs to, if it is working material."""

        return ephemeral_operation(self.name)

    @property
    def manageable(self) -> bool:
        """Whether HQ's model can express this record at all.

        Cloudflare serves types HQ deliberately does not model: SRV, NS, PTR,
        SVCB and more. They are real records in the zone and are listed as such,
        but HQ cannot declare one, so it must never try: adoption runs inside
        the controller sweep, and a spec the model rejects took the whole
        transaction down with it, losing every provider's inventory over one
        delegated subdomain.
        """

        return self.record_type in DNS_RECORD_TYPES_BY_ID

    @property
    def value(self) -> str:
        """The record as one line, the way a zone file would state it."""

        if self.record_type == "MX" and self.priority is not None:
            return f"{self.priority} {self.content}"
        return self.content

    @property
    def url(self) -> str:
        return entity_link("resource", self.resource_key).url

    @property
    def edit_url(self) -> str:
        return (
            reverse("control_plane:edit", kwargs={"key": self.resource_key})
            if self.resource_key
            else ""
        )

    @property
    def remove_url(self) -> str:
        return (
            reverse("control_plane:remove", kwargs={"key": self.resource_key})
            if self.resource_key
            else ""
        )

    @property
    def service_url(self) -> str:
        """Where this name is answered for, when it names something at all."""

        if not self.declares_service:
            return ""
        return entity_link("service", normalized_hostname(self.name)).url


@dataclass(frozen=True)
class ZoneInsight:
    """One thing worth knowing about a domain, and where to go about it.

    ``value`` is the answer and stays short enough to read at a glance, because
    it is set in the card's headline type. Anything needing a sentence goes in
    ``detail``, the caption: an explanation in the headline slot rendered as a
    paragraph of bold text and drowned the cards beside it.

    ``url`` is what makes these worth more than the provider's own dashboard. A
    card that restates a DNS record is Cloudflare with different fonts; one that
    says which HQ certificate covers this domain, and links to it, answers a
    question no single provider can.
    """

    label: str
    value: str
    detail: str = ""
    url: str = ""
    # The full list behind a summarised value, shown in a dialog. A domain with
    # thirty services cannot name them on a card and should not have to lose
    # them to a count: the count is the answer, and the list is one click under
    # it. ``url`` stays required alongside this and must reach a page that does
    # the same job, so the card still works when the dialog does not open.
    rows: tuple[ListRow, ...] = ()
    # A short list on the card itself, one record per line.
    lines: tuple[ListRow, ...] = ()
    # A muted line under the detail: a reason kept available, not an alarm.
    note: str = ""
    # The note's full reason, where the note says it in fewer words.
    note_title: str = ""
    # Reserved for things that are wrong by their own definition rather than by
    # a policy nobody declared: a leftover challenge record is garbage whoever
    # you ask, and a CAA record that forbids the authority HQ renews with will
    # fail a renewal. A permissive DMARC policy is a choice.
    concern: bool = False
    # ``(label, links)``: entities the card names, through the link builder.
    links: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    # Shown as a notice above the records rather than as a card.
    notice: bool = False

    @property
    def modal_id(self) -> str:
        """A dialog id derived from the label, so no caller invents one."""

        return "zone-" + "".join(
            char if char.isalnum() else "-" for char in self.label.lower()
        ).strip("-")


# Each entry is a ``module:attribute`` taking a Zone and returning a
# ZoneInsight, or None when it has nothing to say.
#
# A registry rather than a function with five sections in it, for the same
# reason the providers are one: what is worth knowing about a domain is not a
# fixed list. Analytics and zone posture both arrive as one entry here when the
# credential to read them exists, and the template never changes.
#
# Late-bound as strings so this module keeps deriving and the contributors can
# each import whatever they need (certificates, services) without this file
# depending on all of it.
# Four cards, each answering one question: what runs here, how it is secured,
# its mail, and whether it is still owned. A notice (left-over challenges)
# renders above the records instead of as a card.
ZONE_INSIGHTS: tuple[str, ...] = (
    "application.zone_insights:services",
    "application.zone_insights:security",
    "application.zone_insights:email",
    "application.zone_insights:registration",
    "application.zone_insights:leftover_challenges",
)


@dataclass(frozen=True)
class Zone:
    zone: str
    resource_key: str = ""
    connection_ref: str = ""
    records: tuple[ZoneRecord, ...] = ()
    observed_at: Any = None
    reachable: bool = True
    # Set only while the domain is undeclared, and the handle adoption uses.
    # Blank when the zone is read through a connection that only observes.
    adopt_token: str = ""
    pinned: bool = False
    # Undeclared and read through a connection that only observes.
    observed_only: bool = False

    @property
    def managed(self) -> bool:
        return bool(self.resource_key)

    @property
    def url(self) -> str:
        return entity_link("zone", self.zone).url

    @property
    def managed_count(self) -> int:
        return sum(1 for record in self.records if record.managed)

    @property
    def adoptable(self) -> tuple[ZoneRecord, ...]:
        """Records HQ could declare and has not.

        Machine-owned records are excluded rather than listed as outstanding
        work: they are never going to be adopted, and counting them means the
        page permanently reports something left to do.
        """

        return tuple(
            record for record in self.records
            if not record.managed
            and not record.ephemeral
            and record.manageable
            and not record.observed_only
        )

    @property
    def listed(self) -> tuple[ZoneRecord, ...]:
        """The records worth browsing: everything that is not working material.

        A challenge record lives for seconds. Listing one invites an operator to
        reason about a row that will not exist by the time they have read it,
        and the only case that matters (one that outlived its issuance) is
        reported as an insight instead.
        """

        return tuple(record for record in self.records if not record.ephemeral)

    @property
    def routing(self) -> tuple[ZoneRecord, ...]:
        """Where traffic goes. The question a domain is usually opened to answer."""

        return tuple(record for record in self.listed if not record.secondary)

    @property
    def policy(self) -> tuple[ZoneRecord, ...]:
        """Verification strings and issuance rules, folded away by default."""

        return tuple(record for record in self.listed if record.secondary)

    @property
    def cards(self) -> tuple[ZoneInsight, ...]:
        return tuple(item for item in self.insights if not item.notice)

    @property
    def notices(self) -> tuple[ZoneInsight, ...]:
        return tuple(item for item in self.insights if item.notice)

    @cached_property
    def insights(self) -> tuple[ZoneInsight, ...]:
        """What HQ can say about this domain, asked of each contributor.

        A contributor that fails is skipped rather than taking the page with
        it. This is the screen an operator opens to find out what is wrong, and
        refusing to render it because one card could not be computed is the
        least useful possible moment to fail.
        """

        from .plugins import _import

        found = []
        for reference in ZONE_INSIGHTS:
            try:
                insight = _import(reference)(self)
            except Exception:  # noqa: BLE001 - one card must not lose the page
                continue
            if isinstance(insight, tuple):
                found.extend(insight)
            elif insight is not None:
                found.append(insight)
        return tuple(found)


def _record_of(spec: dict[str, Any], **extra: Any) -> ZoneRecord:
    return ZoneRecord(
        name=normalized_hostname(str(spec.get("name", ""))),
        record_type=str(spec.get("record_type", "")).upper(),
        content=str(spec.get("content", "")),
        priority=spec.get("priority"),
        proxied=bool(spec.get("proxied", False)),
        ttl=spec.get("ttl", 1),
        **extra,
    )


def _sort_key(record: ZoneRecord) -> tuple:
    """Apex first, then by name, then by type.

    A zone read top to bottom should start where a person starts: the domain
    itself, then everything under it. Sorted purely alphabetically, the apex
    lands in the middle of its own subdomains.
    """

    labels = record.name.split(".")
    return (len(labels), record.name, record.record_type, record.content)


def zone_catalog(pinned: frozenset[str] = frozenset()) -> tuple[Zone, ...]:
    """Every domain HQ has been told about, declared or merely seen.

    Undeclared zones are included so one can be adopted from the list. Read
    once per projection.
    """

    from .projection import read_once

    return read_once(
        f"zones.catalog:{','.join(sorted(pinned))}", lambda: _zone_catalog(pinned)
    )


def _zone_catalog(pinned: frozenset[str]) -> tuple[Zone, ...]:

    declared: dict[str, ManagedResource] = {
        normalized_hostname(str(resource.spec.get("zone", ""))): resource
        for resource in ManagedResource.objects.filter(kind=ZONE_KIND, enabled=True)
        if resource.spec.get("zone")
    }

    snapshots = {
        snapshot.kind: snapshot
        for snapshot in ProviderInventory.objects.filter(
            kind__in=(ZONE_KIND, RECORD_KIND)
        )
    }
    zone_snapshot = snapshots.get(ZONE_KIND)
    seen: dict[str, dict[str, Any]] = {}
    if zone_snapshot:
        for entry in zone_snapshot.records:
            name = normalized_hostname(str(entry.get("zone", "")))
            if name:
                seen[name] = entry

    # One pass over the unmanaged set, shared by the zones and their records.
    # Called twice it would run the whole inventory diff twice for one page.
    pending = list(unmanaged())
    zone_items = {
        normalized_hostname(str(item.spec.get("zone", ""))): item
        for item in pending
        if item.kind == ZONE_KIND and item.spec.get("zone")
    }

    by_zone: dict[str, list[ZoneRecord]] = {}
    for resource in ManagedResource.objects.filter(kind=RECORD_KIND, enabled=True):
        zone = normalized_hostname(str(resource.spec.get("zone", "")))
        if not zone:
            continue
        by_zone.setdefault(zone, []).append(
            _record_of(
                resource.spec,
                resource_key=resource.key,
                health=resource_health(resource),
            )
        )
    for item in pending:
        if item.kind != RECORD_KIND:
            continue
        zone = normalized_hostname(str(item.spec.get("zone", "")))
        if not zone:
            continue
        by_zone.setdefault(zone, []).append(
            _record_of(item.spec, token=item.token, observed_only=item.observed_only)
        )

    record_snapshot = snapshots.get(RECORD_KIND)
    zones = []
    for name in zone_names():
        resource = declared.get(name)
        item = None if resource else zone_items.get(name)
        zones.append(
            Zone(
                zone=name,
                resource_key=resource.key if resource else "",
                connection_ref=(
                    str((resource.spec if resource else {}).get("connection_ref", ""))
                    or str(seen.get(name, {}).get("connection_ref", ""))
                ),
                records=tuple(sorted(by_zone.get(name, []), key=_sort_key)),
                observed_at=record_snapshot.observed_at if record_snapshot else None,
                reachable=record_snapshot.reachable if record_snapshot else True,
                adopt_token=item.token if item and not item.observed_only else "",
                pinned=name in pinned,
                observed_only=bool(item and item.observed_only),
            )
        )
    # Pinned first, then alphabetical within each half. Sorted here rather than
    # in the view so every surface that lists domains agrees on the order
    # without restating the rule.
    return tuple(sorted(zones, key=lambda zone: (not zone.pinned, zone.zone)))


def zone_reads(resources=None) -> tuple[tuple[str, ...], dict[str, list[Any]]]:
    """Every domain HQ knows, and when a reachable zone sweep read each one.

    A domain is declared, swept, or holds a declared or swept record. The one
    answer the domains page, the API and the estate count list.
    """

    from .infrastructure import enabled_resources

    names = {
        normalized_hostname((resource.spec or {}).get("zone"))
        for resource in (enabled_resources() if resources is None else resources)
        if resource.kind in (ZONE_KIND, RECORD_KIND)
    }
    read = _swept_zones(names)
    names.update(_swept_record_zones())
    return tuple(sorted(name for name in names if name)), read


def _swept_zones(names: set[str]) -> dict[str, list[Any]]:
    """Add every swept zone to ``names``; when a reachable sweep read each."""

    from control_plane.providers import PROVIDERS

    from .facts import snapshots_of

    read: dict[str, list[Any]] = {}
    zones = PROVIDERS[ZONE_KIND]
    for snapshot in snapshots_of(ZONE_KIND):
        for record in snapshot.records:
            try:
                found = tuple(
                    normalized_hostname(name)
                    for name in zones.identity(zones.from_record(record))
                )
            except (KeyError, TypeError, ValueError):
                continue
            names.update(found)
            if snapshot.reachable:
                for name in found:
                    read.setdefault(name, []).append(snapshot.observed_at)
    return read


def _swept_record_zones() -> set[str]:
    """The zone of every swept record."""

    from control_plane.providers import PROVIDERS

    from .facts import snapshots_of

    records = PROVIDERS[RECORD_KIND]
    found = set()
    for snapshot in snapshots_of(RECORD_KIND):
        for record in snapshot.records:
            try:
                found.add(normalized_hostname(records.from_record(record).get("zone")))
            except (KeyError, TypeError, ValueError):
                continue
    return found


def zone_names() -> tuple[str, ...]:
    """Every domain HQ knows. See ``zone_reads``."""

    return zone_reads()[0]


@dataclass(frozen=True)
class DomainContext:
    """One domain's page: the domain, and every domain for the switcher."""

    zone: Zone
    zones: tuple[Zone, ...]


def domain_context(zone: str, *, pinned: frozenset[str] = frozenset()) -> DomainContext | None:
    """The domain named ``zone`` out of one catalogue build, or None."""

    zones = zone_catalog(pinned=pinned)
    wanted = normalized_hostname(zone)
    found = next((item for item in zones if item.zone == wanted), None)
    return DomainContext(found, zones) if found is not None else None


def find_zone(zone: str) -> Zone | None:
    context = domain_context(zone)
    return context.zone if context is not None else None


@transaction.atomic
def adopt_discovered_records(*, principal) -> dict[str, Any]:
    """Take on every record found in a domain HQ has been made responsible for.

    Run on each controller sweep, so "a record HQ has not adopted yet" is a
    state that closes itself within a minute rather than a chore on a screen.

    There was never a decision in it. Declaring a domain is the decision, and
    it is made once; asking again per record (seventeen times on a working
    zone, and again for every record added at the provider afterwards)
    presented a question whose answer is always yes, and left a page reporting
    outstanding work that nobody intended to do.

    Safe for the reason every adoption is safe: the spec is read back out of
    the live record, so each declaration starts equal to the world and the
    first reconciliation changes nothing. What this cannot do is decide that a
    record should not exist. Removing one stays deliberate and manual.

    Working material is skipped (``EPHEMERAL_PREFIXES``), and so are records
    read through a connection that only observes: see ``application.adoption``.
    """

    from django.core.exceptions import ValidationError

    from .infrastructure import NotFoundError, PolicyError

    adopted: list[str] = []
    for zone in zone_catalog():
        if not zone.managed or not zone.adoptable:
            continue
        try:
            adopted.extend(adopt_zone_records(zone.zone, principal=principal)["adopted"])
        except (NotFoundError, PolicyError, ValidationError, ValueError):
            # Recording what a provider holds must not depend on being allowed
            # to declare it. A deployment with public DNS switched off, or a
            # record whose live shape HQ's model cannot express, leaves the
            # sweep itself intact: losing the whole inventory because one
            # record could not be adopted would be a far worse trade.
            #
            # ValueError is not redundant beside Django's ValidationError:
            # pydantic raises its own, which is a ValueError and nothing else.
            # Without it, one NS record in a declared zone rolled back every
            # provider's inventory on every pass.
            continue
    return {"ok": True, "adopted": adopted}


@transaction.atomic
def adopt_zone_records(zone: str, *, principal) -> dict[str, Any]:
    """Bring every unmanaged record in one domain under management, or none.

    Offered because the alternative is real: a working zone has dozens of
    records and adopting them one at a time is dozens of round trips to say the
    same thing. Atomic for the same reason ``adopt_service`` is: a half
    adopted zone is harder to reason about than an unadopted one, because the
    gap looks like a missing record rather than an unfinished action.

    Safe for the same reason every adoption is safe: each spec is read back out
    of the live record, so the declarations start equal to the world and the
    first reconciliation changes nothing.
    """

    from .infrastructure import NotFoundError
    from .inventory import AdoptCommand, adopt

    found = find_zone(zone)
    if found is None:
        raise NotFoundError(f"No domain named {zone!r}.")
    pending = list(found.adoptable)
    if not pending:
        raise NotFoundError(
            f"All records in {found.zone} are already managed."
        )
    adopted = [
        adopt(
            AdoptCommand(kind=RECORD_KIND, token=record.token), principal=principal
        )["resource"]["key"]
        for record in pending
    ]
    return {"ok": True, "zone": found.zone, "adopted": adopted}


def public_answers_for(hostname: str) -> tuple[str, ...]:
    """What the public internet is told when it asks for this name.

    Read from the zones HQ actually manages rather than by resolving the name.
    A resolver on the same machine as HQ is inside every boundary this is meant
    to describe: it would follow the internal rewrite and report that a
    private name resolves, which is the opposite of the question being asked.

    An empty answer means no zone HQ manages publishes this name. That is not
    the same as proving the name is unresolvable everywhere, and nothing built
    on this should say that it is.
    """

    wanted = normalized_hostname(hostname)
    if not wanted:
        return ()
    found: list[str] = []
    for snapshot in ProviderInventory.objects.filter(kind=RECORD_KIND):
        for record in snapshot.records:
            if normalized_hostname(str(record.get("name", ""))) != wanted:
                continue
            content = str(record.get("content", "")).strip()
            if content and content not in found:
                found.append(content)
    return tuple(found)
