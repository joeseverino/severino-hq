"""The estate at a glance: machines, services, domains and connections.

Read from the catalogs the pages use, once per projection: the machine
catalog, the service catalog, the zone names, the connection rows and the
joined readings. The dashboard card, the estate's action items and the command
center read ``estate_reading``; none keeps a list of its own.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.domains.control_plane.provider_adapters.tls import (
    CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_KIND,
)
from hq.domains.control_plane.provider_spec import expiry_phrase
from hq.platform.application.labels import dotted
from hq.platform.application.routes import reverse
lazy from hq.domains.control_plane.names import in_zone

from .action_links import read_link
from .card_items import Counted, Kpi
from .certificate_expiry import serving
from .connection_lifecycle import Lifecycle
from .drawings import Dot, Dots
from .entity_links import entity_link
from .expiry import DEFAULT_RENEWAL_WINDOW_DAYS, days_until, renewal_window
from .integration_specs import ListRead, ResourceSpec
from .moments import moment, span, when_day
from .projection import read_once
from .resources import EmptyQuery
from .security import Capability, Principal
from .tone import Tone
from .ui import DomainOverview
from .workflow_contracts import ActionLink
lazy from . import connections, derived_reads, services, zones
lazy from .connection_catalog import connection_total
lazy from .connections import connection_rows, connection_standing, credential_refusals
lazy from .estate_records import EstateFigure
lazy from .facts import Subject, inventory_about, readings
lazy from .infrastructure import enabled_resources
lazy from .machines import MachineState, in_states
lazy from .services import service_reading
lazy from .zones import ZONE_KIND

# An edge certificate renews itself well before this; inside it, renewal is failing.
EDGE_RENEWAL_OVERDUE_DAYS = 14
MANAGED_CERTIFICATE_KINDS = (CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND)
# The connection states HQ cannot read through.
_TROUBLED = frozenset({Lifecycle.UNREPORTED, Lifecycle.REVOKED, Lifecycle.UNREACHABLE})


def subject_link(kind: str, name: str) -> ActionLink | None:
    """An insight's subject, linked by the shared entity link builder."""

    if not name:
        return None
    link = entity_link(kind, name)
    return read_link("subject", link.label, link.url) if link.url else None


@dataclass(frozen=True, slots=True)
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
        return days_until(self.expires)

    @property
    def phrase(self) -> str:
        return expiry_phrase(self.expires)

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


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """A connection HQ cannot currently read through."""

    ref: str
    state: Lifecycle
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Estate:
    machines: tuple[Any, ...] = ()
    services: tuple[Any, ...] = ()
    domains: tuple[str, ...] = ()
    connections: tuple[ConnectionState, ...] = ()
    registrations: tuple[Expiry, ...] = ()
    certificates: tuple[Expiry, ...] = ()

    @property
    def offline(self) -> tuple[Any, ...]:
        # A personal device that is off reads as away, never offline.
        return tuple(item for item in self.machines if item.state is MachineState.OFFLINE)

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
    catalog = services.service_catalog()
    domains = zones.zone_names()
    hostnames = {service.hostname for service in catalog}
    return Estate(
        machines=connections.machines_once(),
        services=catalog,
        domains=domains,
        connections=connection_states(),
        registrations=_registrations(domains),
        certificates=_certificates(domains, hostnames),
    )


def connection_states() -> tuple[ConnectionState, ...]:
    return read_once("estate.connections", _connections)


def _connections() -> tuple[ConnectionState, ...]:
    """Connections that did not answer, whose provider refused the credential, or that their controller stopped reporting."""

    found = []
    for row in connection_rows():
        state = connection_standing(row)
        if state in _TROUBLED:
            detail = credential_refusals()[row.provider] if state == Lifecycle.REVOKED else row.detail
            found.append(ConnectionState(row.connection_ref, state, detail))
    return tuple(sorted(found, key=lambda item: item.ref))


def _registrations(domains: tuple[str, ...]) -> tuple[Expiry, ...]:
    """Each domain's registration expiry: the registrar's, else the public registry's."""

    index = readings()
    found = []
    for name in domains:
        subject = Subject.of(hostnames=(name,))
        registration: dict[str, Any] = {}
        for _snapshot, record in inventory_about(ZONE_KIND, subject):
            registration = dict(record.get("registration") or {})
        expires = None if registration.get("unread") else moment(registration.get("expires_at"))
        auto_renew = bool(registration.get("auto_renew")) if expires else None
        if expires is None:
            expires = min(
                (when for item in index.about(subject, facets=("registration",)) if (when := moment(item.expires))),
                default=None,
            )
        if expires is not None:
            found.append(Expiry(name, "Registration", expires, subject_link("zone", name), auto_renew))
    return tuple(sorted(found, key=lambda item: item.expires))


def _certificates(domains: tuple[str, ...], hostnames: set[str]) -> tuple[Expiry, ...]:
    """Edge certificates read under the domains, and certificates HQ manages."""

    found = [*_edge_certificates(domains, hostnames), *managed_certificates()]
    return tuple(sorted(found, key=lambda item: item.expires))


def _edge_certificates(domains: tuple[str, ...], hostnames: set[str]) -> list[Expiry]:
    """Each name a reading holds a certificate for that says when it expires,
    judged by the certificate that serves it, linked to its service when HQ
    serves the name, else to its zone."""

    if not domains:
        return []
    found = []
    for item in readings().about(Subject.of(zones=domains), facets=("certificate",)):
        if (when := moment(item.expires)) is None:
            continue
        name = next(iter(item.hostnames), "") or item.title
        source = item.spec.short or item.label
        found.append(((source, name), when, (name, source, when)))
    expiries = []
    for name, source, when in serving(found):
        zone = next((domain for domain in domains if in_zone(name, domain)), "")
        link = subject_link("service", name) if name in hostnames else subject_link("zone", zone)
        expiries.append(Expiry(name, source, when, link))
    return expiries


def managed_certificates() -> list[Expiry]:
    """Each certificate HQ manages whose status says when it expires."""

    found = []
    for resource in enabled_resources():
        if resource.kind not in MANAGED_CERTIFICATE_KINDS:
            continue
        if (when := moment((resource.status or {}).get("not_after"))) is None:
            continue
        spec = resource.spec or {}
        found.append(
            Expiry(
                str(spec.get("certificate_name") or resource.key),
                "Certificate",
                when,
                subject_link("resource", resource.key),
                resource_key=resource.key,
                renewal_window_days=renewal_window(spec),
            )
        )
    return found


# ----- Dashboard ------------------------------------------------------------


def cards(*, principal: Principal) -> tuple[dict[str, Any], ...]:
    """The estate card as ``principal`` reads it: every figure links to the page that lists it.

    The connection count is the viewer's own, since it runs only the families
    their capabilities permit; every other figure is the shared reading.
    """

    estate = estate_reading()
    if estate.empty:
        return ()
    services = service_reading()
    found = [
        {
            "id": "hq.estate.machines",
            "label": Counted(len(estate.machines), "Machine"),
            "url": reverse("control_plane:machines"),
            "detail": in_states(machine.state for machine in estate.machines),
            **({"status": Tone.ATTENTION} if estate.offline else {}),
        },
        {
            "id": "hq.estate.services",
            "label": Counted(services["total"], "Service"),
            "url": reverse("control_plane:services"),
            **({"detail": f"{services['incomplete']} not fully set up"} if services["incomplete"] else {}),
        },
        {
            "id": "hq.estate.domains",
            "label": Counted(len(estate.domains), "Domain"),
            "url": reverse("zones:index"),
        },
    ]
    # Each figure leads to the thing it is about, and to the list of them
    # when that thing has no page of its own.
    for card_id, label, expiries, listed in (
        ("hq.estate.registration", "Next renewal", estate.registrations, "zones:index"),
        (
            "hq.estate.certificate",
            "Next certificate expiry",
            estate.operator_certificates,
            "control_plane:services",
        ),
    ):
        if expiries:
            first = expiries[0]
            found.append(
                {
                    "id": card_id,
                    "label": label,
                    "value": span(max(first.days, 0)),
                    "url": first.url or reverse(listed),
                    "detail": dotted(first.subject, first.source, "not renewed by its issuer" if first.overdue else ""),
                    **({"status": Tone.ATTENTION} if first.overdue else {}),
                }
            )
    if total := connection_total(principal=principal):
        found.append(_connections_card(estate, total))
    return tuple(found)


def _connections_card(estate: Estate, total: int) -> dict[str, Any]:
    """Every connection, like machines and services; one with a problem says which."""

    troubled = estate.connections
    card = {
        "id": "hq.estate.connections",
        "label": Counted(total, "Connection"),
        "url": reverse("control_plane:connections"),
    }
    if len(troubled) == 1:
        # One leads to its own row; several to the page that lists them.
        card |= {
            "url": entity_link("connection", troubled[0].ref).url,
            "detail": f"{troubled[0].ref} {troubled[0].state.label.lower()}",
        }
    elif troubled:
        card["detail"] = f"{len(troubled)} with a problem"
    return card | ({"status": Tone.ATTENTION} if troubled else {})


def _answers_at(machine) -> str:
    """Where a machine answers, as few addresses as say it.

    Loopback is left out, since it is true of every machine and reaches none of
    them, and an IPv4 address is preferred because it is the one a person
    types. Two at most: which is right depends on where the reader is sitting.
    """

    known = tuple(dict.fromkeys(address for address in (machine.address, *machine.addresses) if address))
    reachable = tuple(address for address in known if not address.startswith("127.") and address != "::1")
    typed = tuple(address for address in reachable if ":" not in address)
    return ", ".join((typed or reachable)[:2])


def overview(*, principal: Principal):
    shown = cards(principal=principal)
    estate = estate_reading()
    # A service is drawn when HQ reads its health, as a machine is when a
    # connection reaches it: one with no health reading (a DNS record only,
    # hosted elsewhere) is neither up nor down, and a hollow dot would say down.
    checked = tuple(service for service in estate.services if service.health.state is not None)
    # A part of a whole is drawn as well as said: one dot a machine or a
    # service, naming it when pointed at and leading to its page. On the
    # overview only: the cards are also a transport payload, which carries no
    # drawing.
    drawings = {
        "hq.estate.machines": Dots(
            tuple(
                Dot(
                    filled=machine.state is MachineState.ONLINE,
                    tip=dotted(machine.name, _answers_at(machine), machine.state),
                    url=entity_link("machine", machine.name).url,
                )
                for machine in estate.machines
            )
        ),
        "hq.estate.services": Dots(
            tuple(
                # Filled for what the services page calls healthy, so a dot is
                # never greener than the State it leads to.
                Dot(
                    filled=service.health.state == Tone.GOOD,
                    tip=dotted(service.hostname, service.faults[0] if service.faults else service.health.label),
                    url=entity_link("service", service.hostname).url,
                )
                for service in checked
            )
        ),
    }
    # One word each, so a label and its mark fit a narrow column; the note
    # carries the rest. For a date that runs out, the note is what and when.
    labels = {"hq.estate.registration": "Renewal", "hq.estate.certificate": "Certificate"}
    notes = {}
    if len(checked) < len(estate.services):
        notes["hq.estate.services"] = f"{len(checked)} health-checked"
    for card_id, expiries in (
        ("hq.estate.registration", estate.registrations),
        ("hq.estate.certificate", estate.operator_certificates),
    ):
        if expiries and not expiries[0].overdue:
            notes[card_id] = dotted(expiries[0].subject, when_day(expiries[0].expires))
    icons = {
        "hq.estate.machines": "server",
        "hq.estate.services": "layers",
        "hq.estate.domains": "globe",
        "hq.estate.registration": "calendar",
        "hq.estate.certificate": "lock",
        "hq.estate.connections": "link",
    }
    return DomainOverview(
        description="Machines, services, domains and connections.",
        url=reverse("control_plane:topology"),
        kpis=tuple(
            Kpi(
                label=labels.get(card["id"], card["label"]),
                value=card.get("value", ""),
                detail=notes.get(card["id"]) or card.get("detail", ""),
                url=card["url"],
                icon=icons.get(card["id"], ""),
                drawing=drawings.get(card["id"]),
            )
            for card in shown
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "estate",
            "Estate",
            "Machines and their states, services, domains, next renewals and connections needing attention.",
            Capability.READ,
            listing=ListRead(derived_reads.list_estate, EmptyQuery, EstateFigure),
            web_route="dashboard",
            pass_principal=True,
        ),
    )
