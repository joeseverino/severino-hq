"""Everything else HQ holds about a machine, gathered by what it already knows.

The machine page is where the estate is most densely related. Behind each name
a machine answers on sit a DNS record, a proxy host, a certificate and something
running, which HQ already resolves for the service page; this asks the service
catalog for them, so the two cannot disagree, and draws them with the service
page's sections and cells, so a band renders identically on both.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from hq.domains.control_plane.names import normalized_hostname
from hq.platform.application.labels import dotted
from hq.platform.application.url_query import with_query
lazy from hq.domains.control_plane.models import ManagedResource
lazy from hq.domains.control_plane.providers import service_facets

from . import docker_sections
from .analytics import HOST_TRAFFIC_DAYS, traffic_for_hosts
from .entity_links import entity_link, kind_label, provider_label
from .moments import Span
from .projection import projection_scope
from .security import Principal
from .service_context import Cell, ServiceSection, recent_changes, recent_work
from .ui import MISSING
lazy from .finding_model import CONTAINERS_CARD, TAILNET_CARD
lazy from .locate import host_of
lazy from .machine_records import MachineRecord
lazy from .machines import Machine
lazy from .own_addresses import tailnet_sightings
lazy from .policy_links import PolicyNames
lazy from .problems import About, problems_about
lazy from .public_registry import public_endpoints
lazy from .reach import network_of
lazy from .read_requests import connection_providers
lazy from .routes import reverse
lazy from .service_list import listed_services, served_on
lazy from .tailnet import TAILNET_KIND
lazy from .topology_model import RELATIONS
lazy from .work_runs import runs_on


def sections_for(machine, *, principal: Principal) -> tuple[ServiceSection, ...]:
    """Every section that has something to say about this machine to ``principal``."""

    found = []
    with projection_scope():
        for resolve in SECTIONS:
            section = resolve(machine, principal=principal) if resolve in _READ_BY else resolve(machine)
            if section is not None and section.things:
                found.append(section)
    return tuple(found)


def _identity(machine) -> ServiceSection | None:
    """The declarations that constitute this machine, and what each is filed as.

    A machine declared twice (as a machine, and as the device the tailnet
    knows) files the second under a suffixed key, since keys are unique across
    kinds, so ``x`` and ``x-2`` read as two machines to anyone who meets the
    second first. A name that looks like a duplicate is worth one row to
    explain and expensive to rediscover.
    """

    keys = list(
        dict.fromkeys(
            key
            for key in (
                machine.declaration,
                machine.route_approval_key,
            )
            if key
        )
    )
    # One declaration is the machine itself, which its page already is: there
    # is nothing to explain until a second one names it.
    if len(keys) < 2:
        return None
    by_key = {r.key: r for r in ManagedResource.objects.filter(key__in=keys)}
    records = tuple(
        (
            Cell.of(entity_link(by_key[key].kind if key in by_key else "resource", key)),
            Cell(kind_label(by_key[key].kind) if key in by_key else MISSING, muted=key not in by_key),
            Cell(
                "Its key ends in a number because the plain key was already in use"
                if key.rpartition("-")[2].isdigit() and not key.rpartition("-")[2].startswith("0")
                else "",
                muted=True,
            ),
        )
        for key in keys
    )
    return ServiceSection(
        id="identity",
        label="Records in HQ",
        columns=("Record", "Type", "Note"),
        records=records,
    )


# What supplies a name, as this page heads each column.
_SUPPLIED_BY = {"runtime": "Container", "dns": "DNS record", "proxy": "Proxy", "certificate": "Certificate"}


def _names(machine, *, principal: Principal) -> ServiceSection | None:
    """Every name this machine answers on, and what supplies each one.

    The band the machine page exists for: what runs, resolves, fronts and
    secures each name, read from the service page's decision rather than made a
    second time. Traffic rides along because four green cells beside a name
    nobody visits differ from four beside the name carrying the site, and only
    this page holds enough names at once for the comparison to mean anything.
    """

    hostnames = served_on(machine.name)
    if not hostnames:
        return None

    catalog = {service.hostname: service for service in listed_services()}
    measured = traffic_for_hosts(set(hostnames), days=HOST_TRAFFIC_DAYS)
    facets = service_facets()

    # A blank cell is "nothing supplies this", which is neither unhealthy nor
    # unmeasured; each is said in its own words.
    def supplied(service, facet_id: str) -> Cell:
        facet = next((f for f in service.facets if f.id == facet_id), None) if service else None
        claim = next(iter(facet.claims), None) if facet and facet.present else None
        if claim is None:
            return Cell(MISSING, muted=True)
        # The row is already the name, so the cell says where it leads, and,
        # while what it leads to has an open problem, the worst of them in its
        # tone, as the queue words it less the name the cell already shows.
        link = claim.within_service
        worst = next(iter(problems_about(link.url, principal)), None)
        if worst is None:
            return Cell.of(link)
        return Cell(worst["label"].removeprefix(link.label).strip(), link.url, tone=worst["status"], link=link)

    def traffic(hostname: str) -> Cell:
        reading = measured.get(normalized_hostname(hostname))
        if not reading:
            return Cell("unmeasured", muted=True)
        return Cell(f"{reading['pageviews']:,}", sort=str(reading["pageviews"]))

    ordered = sorted(
        hostnames,
        key=lambda host: (
            -(measured.get(normalized_hostname(host), {}).get("pageviews") or -1),
            host,
        ),
    )
    # Traffic rides along once at least one name carries a reading, because
    # that is when the comparison means something. A machine nothing measures
    # keeps its names and loses the column: nine cells all saying "unmeasured"
    # say one thing nine times, and the reachable-but-unmeasured finding is
    # where that absence is raised as a fact about HQ.
    any_measured = any(measured.get(normalized_hostname(host)) for host in hostnames)
    label = "Serves"
    if any_measured:
        label = dotted(label, f"traffic over {Span(timedelta(days=HOST_TRAFFIC_DAYS))}")
    return ServiceSection(
        id="names",
        label=label,
        columns=(
            "Name",
            *(_SUPPLIED_BY.get(facet, facet_label) for facet, facet_label in facets),
            *(("Pageviews",) if any_measured else ()),
        ),
        records=tuple(
            (
                Cell.of(entity_link("service", hostname)),
                *(supplied(catalog.get(hostname), facet) for facet, _label in facets),
                *((traffic(hostname),) if any_measured else ()),
            )
            for hostname in ordered
        ),
    )


def _activity(machine, *, principal: Principal | None) -> ServiceSection | None:
    """What has recently happened to the things this machine holds.

    Audit entries name the object they changed, and the objects on a machine are
    its declarations, so the tie is the key each already carries. Scoped to
    those keys rather than the whole log: this answers "what changed here", not
    "what changed".
    """

    return recent_changes(list(machine.resources), limit=8, principal=principal)


def _runs(machine, *, principal: Principal | None) -> ServiceSection | None:
    """The latest automation runs that acted on this machine, with what
    ``principal`` may read of each.

    A run names its machines as they are canonically spelled, which is how
    its page's name is matched; every run of them is on Jobs.
    """

    name = normalized_hostname(machine.name)
    every = with_query(reverse("jobs:list"), {"host": name})
    return recent_work(runs_on(name), by_machine=True, more_url=every, principal=principal)


def serves_links(machine, relationships) -> tuple:
    """What the machine serves, read off the same relation query as its
    Relationships panel, so the header and the panel cannot disagree. HQ's
    own names lead where HQ runs here."""

    group = relationships.group(RELATIONS["runs_on"].inverse)
    serves = tuple(item.entity for item in group.items) if group else ()
    return tuple(sorted(serves, key=lambda link: link.identity not in machine.hq_hostnames))


def machine_links(record: MachineRecord) -> dict[str, object]:
    """Every entity the machine page names outside its sections, as links,
    from its record: what it is reached through, its tailnet device, and who
    the tailnet policy admits to it. A policy alias names an address; it
    links to the machine answering there.
    """

    presence = record["presence"]
    links: dict[str, object] = {
        "reached_links": tuple(
            (_reached_through(record, ref), ref in record["unanswered"]) for ref in record["reached_by"]
        ),
        "labeled_addresses": (),
        "tailscale_version": "",
        "device_link": None,
        "magic_dns_link": None,
        "opening_links": (),
    }
    if presence is None:
        return links
    links["labeled_addresses"] = labeled_addresses(record)
    # The release, without the build it was cut from.
    links["tailscale_version"] = presence["client_version"].partition("-")[0]
    addresses = {"addresses": [presence["tailnet_address"]]}
    links["device_link"] = (
        entity_link(TAILNET_KIND, record["tailnet_device"], label=presence["tailnet_name"])
        if record["tailnet_device"]
        else entity_link(TAILNET_KIND, presence["tailnet_name"], record=addresses)
    )
    if presence["dns_name"]:
        links["magic_dns_link"] = entity_link(TAILNET_KIND, presence["dns_name"], record=addresses)
    names = PolicyNames()
    links["opening_links"] = tuple((item, names.of(item["open_to"])) for item in presence["openings"])
    return links


def problem_parts(record: MachineRecord) -> dict[str, About]:
    """Where a machine's page draws its problems (``problems_by_part``): under
    each card the page draws that a rule declares, and the rest above the page
    with the machine's own state (``page``)."""

    url = (record["url"],)
    drawn = {CONTAINERS_CARD: bool(record["containers"]), TAILNET_CARD: record["presence"] is not None}
    cards = {card: About(urls=url, cards=(card,)) for card, shown in drawn.items() if shown}
    return {**cards, "page": About(urls=url)}


def _reached_through(record: MachineRecord, ref: str):
    """A connection that reaches the machine, as its page names it.

    A connection named for the machine it reaches would read, on that
    machine's page, as the machine being its own source: it is named by its
    kind instead ("SSH"), its own name kept for hover.
    """

    own = {str(name).lower() for name in (record["name"], *record["aliases"])}
    providers = connection_providers(ref) if ref.lower() in own else ()
    if not providers:
        return entity_link("connection", ref)
    kinds = ", ".join(provider_label(provider) for provider in providers)
    return replace(entity_link("connection", ref, label=kinds), title=ref)


_NETWORKS = {"tailnet": "Tailnet", "network": "LAN", "public": "Public"}


def labeled_addresses(record: MachineRecord) -> tuple[tuple[str, str], ...]:
    """``(label, address)`` for each address the machine is reached at, by the
    network it is on, each once.

    An endpoint's port belongs to the connection that reaches it there, so
    only its host is listed. A loopback address places no machine, and the
    machine's own names say nothing about where it is.
    """

    own = {name.lower() for name in (record["name"], *record["aliases"])}
    hosts = dict.fromkeys(host_of(address) for address in record["addresses"])
    return tuple(
        (_label(host), host) for host in hosts if host and host.lower() not in own and network_of(host) != "loopback"
    )


def _label(host: str) -> str:
    """The network an address is on, and IPv6 said, since a device has one of
    each on the same network; a name is a name."""

    network = network_of(host)
    if not network:
        return "Name"
    return f"{_NETWORKS[network]} IPv6" if ":" in host else _NETWORKS[network]


def header_addresses(machine: Machine) -> tuple[tuple[str, str, str], ...]:
    """``(label, address, holder)`` for the addresses a person uses to reach it.

    Tailnet, then LAN, then public: each only when it is the device's own
    (``application.own_addresses``).
    """

    presence = machine.presence
    if presence is None:
        return ()
    seen = tailnet_sightings()
    lan = seen.lan_address(presence)
    return (
        ((("Tailnet", presence.tailnet_address, ""),) if presence.tailnet_address else ())
        + ((("LAN", lan, ""),) if lan else ())
        + tuple(("Public", address, holder) for address, holder in public_endpoints(seen.public_addresses(presence)))
    )


# The list of sections, stated once: each is a function from a machine (and,
# for one in ``_READ_BY``, its reader) to rows,
# so the page grows when HQ learns something rather than when someone edits it.
# A section with nothing to say returns nothing and does not appear.
SECTIONS: tuple[Callable[..., ServiceSection | None], ...] = (
    _identity,
    _names,
    *docker_sections.SECTIONS,
    _runs,
    _activity,
)
# The sections whose rows depend on who reads them, each handed the reader.
_READ_BY = frozenset({_names, _runs, _activity})
