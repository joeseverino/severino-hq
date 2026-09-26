"""The estate at a glance: machines, services, domains and connections.

Read from the catalogues the pages use, once per projection: the machine
catalogue, the service catalogue, the zone names, the connection rows and the
joined readings. The dashboard card, the estate's action items and the command
center read ``estate_reading``; none keeps a list of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.urls import reverse
from django.utils import timezone

from control_plane.providers import (
    CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_KIND,
    expiry_phrase,
)

from .entity_links import entity_link
from .projection import read_once
from .ui import Insight, Kpi, ago, counted
from .workflow_contracts import ActionLink

# A machine that holds something for the estate and has been offline this long
# is an action item. Shorter gaps are restarts.
OFFLINE_AFTER = timedelta(hours=1)
# Used when a certificate declares no renewal window of its own.
DEFAULT_RENEWAL_WINDOW_DAYS = 30
CERTIFICATE_SERIOUS_DAYS = 7
# An edge certificate renews itself well before this; inside it, renewal is failing.
EDGE_RENEWAL_OVERDUE_DAYS = 14
MANAGED_CERTIFICATE_KINDS = (CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND)
REFUSED = "refused"
NOT_ANSWERING = "not answering"


def subject_link(kind: str, name: str) -> ActionLink | None:
    """An insight's subject, linked by the shared entity link builder."""

    if not name:
        return None
    link = entity_link(kind, name)
    return ActionLink("subject", link.label, "read", link.url) if link.url else None


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        found = value
    else:
        try:
            found = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    return found if found.tzinfo else found.replace(tzinfo=dt_timezone.utc)


@dataclass(frozen=True)
class Expiry:
    """One date something runs out, and whose page says more."""

    subject: str
    source: str
    expires: datetime
    link: ActionLink | None = None
    # A registration's auto-renew, when the registrar was read.
    auto_renew: bool | None = None
    # A managed certificate's declaration and renewal window.
    resource_key: str = ""
    renewal_window_days: int = DEFAULT_RENEWAL_WINDOW_DAYS

    @property
    def days(self) -> int:
        return (self.expires - timezone.now()).days

    @property
    def phrase(self) -> str:
        return expiry_phrase(self.expires.isoformat())

    @property
    def url(self) -> str:
        return self.link.url if self.link else ""

    @property
    def managed(self) -> bool:
        """Renewed by the operator, through a declaration HQ holds."""

        return bool(self.resource_key)

    @property
    def overdue(self) -> bool:
        """A certificate its provider renews, inside the window it should have."""

        return not self.managed and self.days <= EDGE_RENEWAL_OVERDUE_DAYS

    @property
    def needs_operator(self) -> bool:
        return self.managed or self.overdue


@dataclass(frozen=True)
class ConnectionState:
    """A connection HQ cannot currently read through."""

    ref: str
    state: str
    detail: str = ""


@dataclass(frozen=True)
class Estate:
    machines: tuple[Any, ...] = ()
    services: tuple[Any, ...] = ()
    domains: tuple[str, ...] = ()
    connections: tuple[ConnectionState, ...] = ()
    registrations: tuple[Expiry, ...] = ()
    certificates: tuple[Expiry, ...] = ()

    @property
    def online(self) -> tuple[Any, ...]:
        return tuple(item for item in self.machines if item.state[0] == "online")

    @property
    def offline(self) -> tuple[Any, ...]:
        return tuple(item for item in self.machines if item.state[0] == "offline")

    @property
    def empty(self) -> bool:
        return not (self.machines or self.services or self.domains or self.connections)

    @property
    def operator_certificates(self) -> tuple[Expiry, ...]:
        """Certificates the operator renews, and provider-renewed ones that did not renew."""

        return tuple(item for item in self.certificates if item.needs_operator)


def estate_reading() -> Estate:
    return read_once("estate.reading", _estate)


def _estate() -> Estate:
    from .connections import machines_once
    from .services import service_catalog
    from .zones import zone_names

    services = service_catalog()
    domains = zone_names()
    hostnames = {service.hostname for service in services}
    return Estate(
        machines=machines_once(),
        services=services,
        domains=domains,
        connections=connection_states(),
        registrations=_registrations(domains),
        certificates=_certificates(domains, hostnames),
    )


def connection_states() -> tuple[ConnectionState, ...]:
    return read_once("estate.connections", _connections)


def refused_connections() -> dict[str, str]:
    """Connection ref to the provider's reason, for each refused credential."""

    return {item.ref: item.detail for item in connection_states() if item.state == REFUSED}


def _connections() -> tuple[ConnectionState, ...]:
    """Connections that did not answer, or whose provider refused the credential."""

    from control_plane.observations import OBSERVATIONS
    from control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL

    from .connections import connection_rows
    from .facts import snapshots_of

    refused = {
        spec.provider: snapshot.error
        for kind, spec in OBSERVATIONS.items()
        for snapshot in snapshots_of(kind)
        if not snapshot.reachable and snapshot.refusal == CREDENTIAL_REFUSAL
    }
    found = []
    for row in connection_rows():
        if row.provider in refused:
            found.append(ConnectionState(row.connection_ref, REFUSED, refused[row.provider]))
        elif not row.reachable:
            found.append(ConnectionState(row.connection_ref, NOT_ANSWERING, row.detail))
    return tuple(sorted(found, key=lambda item: item.ref))


def _registrations(domains: tuple[str, ...]) -> tuple[Expiry, ...]:
    """Each domain's registration expiry: the registrar's, else the public registry's."""

    from .facts import Subject, inventory_about, readings
    from .zones import ZONE_KIND

    index = readings()
    found = []
    for name in domains:
        subject = Subject.of(hostnames=(name,))
        registration: dict[str, Any] = {}
        for _snapshot, record in inventory_about(ZONE_KIND, subject):
            registration = dict(record.get("registration") or {})
        expires = None if registration.get("unread") else _moment(registration.get("expires_at"))
        auto_renew = bool(registration.get("auto_renew")) if expires else None
        if expires is None:
            expires = min(
                (
                    when
                    for item in index.about(subject, facets=("registration",))
                    if (when := _moment(item.expires))
                ),
                default=None,
            )
        if expires is not None:
            found.append(
                Expiry(name, "Registration", expires, subject_link("zone", name), auto_renew)
            )
    return tuple(sorted(found, key=lambda item: item.expires))


def _certificates(domains: tuple[str, ...], hostnames: set[str]) -> tuple[Expiry, ...]:
    """Edge certificates read under the domains, and certificates HQ manages."""

    from control_plane.names import in_zone

    from .facts import Subject, readings
    from .infrastructure import enabled_resources

    found = []
    if domains:
        for item in readings().about(Subject.of(zones=domains), facets=("certificate",)):
            when = _moment(item.expires)
            if when is None:
                continue
            name = next(iter(item.hostnames), "") or item.title
            zone = next((domain for domain in domains if in_zone(name, domain)), "")
            link = (
                subject_link("service", name)
                if name in hostnames
                else subject_link("zone", zone)
            )
            found.append(Expiry(name, item.spec.short or item.label, when, link))
    for resource in enabled_resources():
        if resource.kind not in MANAGED_CERTIFICATE_KINDS:
            continue
        when = _moment((resource.status or {}).get("not_after"))
        if when is None:
            continue
        spec = resource.spec or {}
        found.append(
            Expiry(
                str(spec.get("certificate_name") or resource.key),
                "Certificate",
                when,
                subject_link("resource", resource.key),
                resource_key=resource.key,
                renewal_window_days=int(
                    spec.get("renewal_window_days") or DEFAULT_RENEWAL_WINDOW_DAYS
                ),
            )
        )
    return tuple(sorted(found, key=lambda item: item.expires))


# ----- Dashboard ------------------------------------------------------------


def cards() -> tuple[dict[str, Any], ...]:
    """The estate card: every figure links to the page that lists it."""

    from .services import service_reading

    estate = estate_reading()
    if estate.empty:
        return ()
    services = service_reading()
    found = [
        {
            "id": "hq.estate.machines",
            "label": "Machines online",
            "value": str(len(estate.online)),
            "url": reverse("control_plane:machines"),
            "detail": (
                f"{len(estate.offline)} offline"
                if estate.offline
                else f"of {len(estate.machines)}"
            ),
            **({"status": "attention"} if estate.offline else {}),
        },
        {
            "id": "hq.estate.services",
            "label": "Services",
            "value": str(services["total"]),
            "url": reverse("control_plane:services"),
            **(
                {"detail": f"{services['incomplete']} incompletely wired"}
                if services["incomplete"]
                else {}
            ),
        },
        {
            "id": "hq.estate.domains",
            "label": "Domains",
            "value": str(len(estate.domains)),
            "url": reverse("zones:index"),
        },
    ]
    for card_id, label, expiries in (
        ("hq.estate.registration", "Next renewal", estate.registrations),
        ("hq.estate.certificate", "Next certificate expiry", estate.operator_certificates),
    ):
        if expiries:
            first = expiries[0]
            found.append(
                {
                    "id": card_id,
                    "label": label,
                    "value": counted(max(first.days, 0), "day"),
                    "url": first.url,
                    "detail": f"{first.subject} · {first.source}"
                    + (" · not renewed by its provider" if first.overdue else ""),
                    **({"status": "attention"} if first.overdue else {}),
                }
            )
    found.append(
        {
            "id": "hq.estate.connections",
            "label": "Connections needing attention",
            "value": str(len(estate.connections)),
            "url": reverse("control_plane:connections"),
            **(
                {
                    "detail": f"{estate.connections[0].ref} {estate.connections[0].state}",
                    "status": "attention",
                }
                if estate.connections
                else {}
            ),
        }
    )
    return tuple(found)


def overview():
    from .ui import DomainOverview

    shown = cards()
    return DomainOverview(
        description="Machines, services, domains and connections.",
        url=reverse("control_plane:topology"),
        kpis=tuple(
            Kpi(
                label=card["label"],
                value=card["value"],
                detail=card.get("detail", ""),
                url=card["url"],
                is_zero=card["value"] == "0",
            )
            for card in shown
        ),
    )


# ----- Action items ---------------------------------------------------------


def attention() -> tuple[Insight, ...]:
    """Offline machines the estate depends on, and certificates the operator must renew.

    A connection that does not answer is the ``connection-not-answering``
    finding; a domain that will not renew is ``registration-lapsing``.
    """

    estate = estate_reading()
    return (*_offline(estate), *_expiring(estate))


def _holds_something(machine) -> bool:
    return bool(
        machine.declaration or machine.hostnames or machine.containers or machine.roles
        or machine.runs_hq
    )


def _offline(estate: Estate) -> tuple[Insight, ...]:
    now = timezone.now()
    items = []
    for machine in estate.machines:
        presence = machine.presence
        if presence is None or presence.online or not _holds_something(machine):
            continue
        seen = _moment(presence.last_seen)
        if seen is None or seen.year < 2000 or now - seen < OFFLINE_AFTER:
            continue
        serves = bool(machine.hostnames or machine.roles or machine.runs_hq)
        what = ", ".join(role.label for role in machine.roles)
        items.append(
            Insight(
                status="serious" if serves else "attention",
                eyebrow="Machines",
                key=f"estate-offline:{machine.name}",
                title=f"{machine.name} is offline",
                value="1",
                body=(
                    f"Last seen {ago(seen)}."
                    + (f" It is the {what}." if what else "")
                    + (
                        f" It serves {counted(len(machine.hostnames), 'name')}."
                        if machine.hostnames
                        else ""
                    )
                ),
                action="Open machine",
                url=machine.url,
                subject=subject_link("machine", machine.name),
            )
        )
    return tuple(items)


def _expiring(estate: Estate) -> tuple[Insight, ...]:
    items = []
    for expiry in estate.operator_certificates:
        if expiry.managed and expiry.days > expiry.renewal_window_days:
            continue
        days = max(expiry.days, 0)
        if expiry.managed:
            key = f"estate-certificate:{expiry.resource_key}"
            body = (
                f"Expires {expiry.phrase}. Renewal opens at "
                f"{counted(expiry.renewal_window_days, 'day')}; renew it or check why "
                "renewal did not run."
            )
            action = "Open certificate"
        else:
            key = f"estate-certificate:{expiry.source}:{expiry.subject}"
            body = (
                f"Expires {expiry.phrase}. Its provider renews it automatically and "
                f"should have by now; check the {expiry.source.lower()} certificate "
                "settings."
            )
            action = "Open"
        items.append(
            Insight(
                status="serious" if days <= CERTIFICATE_SERIOUS_DAYS else "attention",
                eyebrow="Certificates",
                key=key,
                title=(
                    f"{expiry.subject} expires in {counted(days, 'day')}"
                    if expiry.days >= 0
                    else f"{expiry.subject} has expired"
                ),
                value=str(days),
                body=body,
                action=action,
                url=expiry.url,
                subject=expiry.link,
            )
        )
    return tuple(items)


