"""Where an address is served: the machine it belongs to and, when certain, the container answering on it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from control_plane.models import ManagedResource, ProviderInventory
from control_plane.provider_adapters.portainer import CONTAINER_KIND

from .entity_links import EntityLink, entity_link
from .infrastructure import declared_machines
from .locate import Machines, host_of, machines_index, split_endpoint


@dataclass(frozen=True)
class Origin:
    """Where a request for this hostname is finally served."""

    address: str
    host: str = ""
    container: str = ""
    # Whether an ingress declared this, or a record merely pointed here. Both
    # are origins and they are not the same sentence: an ingress *forwards* to
    # somewhere, while a record says the name simply answers there. Rendered
    # from one wording, a name with no ingress at all was told that its ingress
    # forwards somewhere: directly beneath its own Ingress card reading "not
    # declared", on the same page.
    #
    # Defaults true because every origin that existed before a record could
    # declare one came from an ingress, and because the sentence it selects is
    # the one those origins have always been rendered with.
    routed: bool = True
    # What readings joined to the origin name as serving it: a Pages project,
    # the holder of a public address.
    serving: tuple[str, ...] = ()

    @property
    def parked(self) -> bool:
        """Whether the name points at a documentation address, where nothing answers."""

        from .reach import is_documentation

        return not self.known and is_documentation(host_of(self.address))

    @property
    def external(self) -> bool:
        """Whether this is served somewhere HQ does not reach.

        A proxy forwards to ``host:port`` by construction; a DNS record names a
        target with no port. So an address with no port came from the record
        itself, which means the name is answered outside this network: a
        Pages site, a mail host, someone else's server.

        Worth separating from "unknown host", which is the same missing lookup
        with a very different meaning: an ingress pointing at an address no host
        claims is a thing HQ cannot describe and probably should.

        Read through the shared endpoint parser rather than by looking for a
        colon. A bare IPv6 answer is full of colons and carries no port at all,
        and counting them called it an ingress: after which the address was
        split at its last colon and matched against nothing.

        The absent port is necessary and was briefly taken as sufficient, which
        is only true while the records that name an origin are public ones. An
        internal rewrite names an origin too, and it names a *private* address:
        no port, no machine HQ happens to have been told about, and read on
        punctuation alone that came out as "served outside this network" for a
        name served one subnet away. The page then withdrew its offer to add an
        ingress, on the grounds that a name answered elsewhere needs nothing
        here, which is the right rule applied to the wrong reading.

        So the question is asked of the address rather than of its spelling.
        Where an address lives is ``reach``'s to answer and it already does, for
        the badge on this same page; a private, tailnet or loopback answer is
        inside by definition, and an unknown host inside the network is what
        ``qualifier`` exists to say. A name rather than an address (a CNAME to
        somewhere that hosts pages) classifies as nothing and stays external,
        which is the case this property was written for.
        """

        from .reach import network_of

        if self.known or split_endpoint(self.address)[1]:
            return False
        return network_of(host_of(self.address)) not in (
            "network",
            "tailnet",
            "loopback",
        )

    @property
    def operator(self) -> str:
        """What a person calls whoever serves this, read off the name itself."""

        from .known_hosts import operator

        return operator(self.address) if self.external else ""

    @property
    def known(self) -> bool:
        """Whether the address belongs to a machine HQ knows.

        An ingress forwarding to an address no host claims is not necessarily
        broken, but it is somewhere HQ cannot describe, reconcile or reach, and
        that is worth saying out loud rather than printing a bare IP.
        """

        return bool(self.host)

    @property
    def label(self) -> str:
        if self.container:
            return f"{self.host} · {self.container}"
        return self.host or self.address

    @property
    def headline(self) -> str:
        """What to call whatever serves this, in one phrase.

        Here rather than in a template, because there are two templates and one
        fact. Phrased in each, they drift, and the board and the page disagree
        about the same origin.
        """

        if self.parked:
            return "Parked"
        if self.external:
            named = " · ".join(self.serving)
            # An address names no operator; only a reading can.
            operator = "" if self.operator == host_of(self.address) else self.operator
            if operator and named:
                return f"{operator} · {named}"
            return operator or named or self.address
        return self.label

    @property
    def qualifier(self) -> str:
        """The caveat, when the headline needs one."""

        return "" if self.parked or self.external or self.known else "unknown host"


@dataclass(frozen=True)
class MachineLink:
    """A machine named on a card, and the page for it.

    Built from the origin and the machines the caller already holds. Looking the
    machine up instead would be a catalogue read per service, which on a board
    is a catalogue read per row.
    """

    name: str
    role: str = ""

    @property
    def url(self) -> str:
        return self.link.url

    @property
    def link(self) -> EntityLink:
        return entity_link("machine", self.name)


def machine_link(
    address: str,
    machines: "tuple[dict[str, Any], ...] | None" = None,
    at: "Whereabouts | None" = None,
) -> "MachineLink | None":
    """The machine an address belongs to, resolved the way a service resolves it.

    One resolution, so a page naming where something runs and a page naming what
    runs there cannot disagree about which machine that is.

    A page asking this of every row passes the readings in, because taken here
    they are three queries per row for facts that are the same on all of them.
    """

    machines = declared_machines() if machines is None else machines
    origin = locate(address, machines, at)
    if not origin.host:
        return None
    return machine_for(origin, machines)


def machine_for(origin: "Origin | None", machines: "tuple[dict[str, Any], ...]"):
    """The machine whatever supplies this facet runs on."""

    if origin is None or not origin.host:
        return None
    role = next(
        (
            str(machine.get("role", ""))
            for machine in machines
            if str(machine.get("name", "")) == origin.host
        ),
        "",
    )
    return MachineLink(name=origin.host, role=role)


class Whereabouts:
    """What places an address: whose machine it is, and what answers there.

    Both are estate-wide readings that every address in a pass shares, and both
    were taken per address: resolving one read the connections, and asking what
    was listening on it read the container sweep and the container
    declarations. A catalogue of thirty names paid for all three thirty times,
    and the loopback case paid once per declared machine on top.

    Read at most once each and only if asked, for the same reason as the
    certificates below: a dashboard listing no service resolves no address, and
    a query nobody needs is one every page pays for.
    """

    def __init__(self, machines: "tuple[dict[str, Any], ...]"):
        self._machines = machines
        self._index: "Machines | None" = None
        self._answering: "dict[tuple[str, Any], list[str]] | None" = None
        self._hosting: "dict[str, list[str]] | None" = None

    def machine_index(self) -> "Machines":
        if self._index is None:
            self._index = machines_index(self._machines)
        return self._index

    def answering(self) -> "dict[tuple[str, Any], list[str]]":
        if self._answering is None:
            self._answering = _answering()
        return self._answering

    def hosting(self) -> "dict[str, list[str]]":
        if self._hosting is None:
            self._hosting = _containers_by_name()
        return self._hosting


def whereabouts(machines: "tuple[dict[str, Any], ...] | None" = None) -> Whereabouts:
    """The placements a page resolving more than one address should read once.

    Passed to ``locate`` and to ``machine_link``. Left out, each of them reads
    what it needs and throws it away, which is right for a single lookup and is
    a query budget that grows with the estate in a loop.
    """

    return Whereabouts(declared_machines() if machines is None else machines)


def locate(
    address: str,
    machines: "tuple[dict[str, Any], ...] | None" = None,
    at: "Whereabouts | None" = None,
    near: str = "",
) -> Origin:
    """Match a forwarding address to a machine, and if certain, a container.

    Which machine an address belongs to is ``application.locate``'s question
    and is asked of it here rather than answered again. What is left is the
    part that is genuinely about a *service*: the loopback case, where the
    address deliberately names no machine, and the container, which is observed
    rather than declared.

    The container is named only when exactly one claims the port. Ambiguity is
    reported as silence: a guess printed beside four facts reads as a fifth.

    ``at`` is passed by anything resolving more than one address, because both
    readings behind this are estate-wide. Left out, they are taken here, which
    is right for a single lookup and wrong in a loop.

    ``near`` is the machine the forwarding proxy runs on: a container name on a
    docker network resolves there first, since that network is on that machine.
    """

    machines = declared_machines() if machines is None else machines
    at = at if at is not None else Whereabouts(machines)
    host_address, port = split_endpoint(address)
    # A proxy forwarding to loopback is forwarding to itself: the request never
    # leaves the machine the ingress runs on. No machine is declared at
    # 127.0.0.1 (every machine is) so matching by address cannot answer it,
    # and the honest answer is the machine with something listening on that
    # port. Silence when more than one qualifies, for the reason above.
    if _is_loopback(host_address):
        found = [
            (machine, claimed)
            for machine in machines
            if (claimed := _listening(str(machine.get("name", "")), port, at))
        ]
        if len(found) == 1:
            machine, claimed = found[0]
            return Origin(
                address=address,
                host=str(machine.get("name", "")),
                container=claimed[0] if len(claimed) == 1 else "",
            )
        return Origin(address=address)
    name = at.machine_index().resolve(host_address)
    if not name:
        # Not an address at all, but a container name on a docker network,
        # which is how everything behind a proxy sharing that network is
        # addressed, and what a Caddy route hands off to. HQ sweeps containers,
        # so the name is not opaque: it names something HQ can already see, and
        # the machine running it is the answer to where this is served.
        #
        # Silent when more than one machine runs that name, for the reason every
        # other ambiguity here is silent: a guess printed beside four facts reads
        # as a fifth.
        hosts = _hosting(host_address, at)
        if near and near in hosts:
            return Origin(address=address, host=near, container=host_address)
        if len(hosts) == 1:
            return Origin(address=address, host=hosts[0], container=host_address)
        # Nothing HQ holds says what is there. The address stays on the page,
        # which is the true answer and the one an operator can act on.
        return Origin(address=address)
    claimed = _listening(name, port, at)
    return Origin(
        address=address,
        host=name,
        container=claimed[0] if len(claimed) == 1 else "",
    )


def _is_loopback(address: str) -> bool:
    """Whether an address means "this machine", by the ranges rather than a name."""

    from .reach import network_of

    return network_of(address) == "loopback"


def _listening(host: str, port: str, at: "Whereabouts | None" = None) -> list[str]:
    """Containers answering on one port of one machine, seen or declared.

    A container sharing the machine's network publishes nothing for Docker to
    report, so a sweep cannot find it by port and the declaration is the only
    thing that can say. Both are read, because a machine can be running one of
    each and the answer must not depend on which.
    """

    if not host or not port.isdigit():
        return []
    found = at.answering() if at is not None else _answering()
    return found.get((host, int(port)), [])


def _hosting(container: str, at: "Whereabouts | None" = None) -> list[str]:
    """Machines running a container of this name, seen or declared."""

    if not container:
        return []
    found = at.hosting() if at is not None else _containers_by_name()
    return found.get(container, [])


def _containers_by_name() -> dict[str, list[str]]:
    """Which machines run a container of each name.

    The inverse of the index below, and read from the same two tables, because
    a forwarding target is sometimes a port on a machine and sometimes the name
    of the thing itself.
    """

    found: dict[str, set[str]] = {}
    for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
        for record in snapshot.records:
            host = str(record.get("host", "") or "")
            name = str(record.get("name", "") or "")
            if host and name:
                found.setdefault(name, set()).add(host)
    for spec in ManagedResource.objects.filter(
        kind=CONTAINER_KIND, enabled=True
    ).values_list("spec", flat=True):
        host = str(spec.get("host", "") or "")
        name = str(spec.get("name", "") or "")
        if host and name:
            found.setdefault(name, set()).add(host)
    return {name: sorted(hosts) for name, hosts in found.items()}


def _answering() -> dict[tuple[str, Any], list[str]]:
    """Every container answering on a port of a machine, by that pair.

    Built whole rather than asked per address. The same two tables answer every
    such question in a pass, and read per question they were the largest part
    of what a service catalogue spent.
    """

    found: dict[tuple[str, Any], set[str]] = {}

    def note(host: Any, name: Any, ports: Any) -> None:
        host = str(host or "")
        name = str(name or "")
        if not host or not name:
            return
        for port in ports or ():
            found.setdefault((host, port), set()).add(name)

    for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
        for record in snapshot.records:
            note(record.get("host"), record.get("name"), record.get("ports"))
    for spec in ManagedResource.objects.filter(
        kind=CONTAINER_KIND, enabled=True
    ).values_list("spec", flat=True):
        note(spec.get("host"), spec.get("name"), spec.get("serves_ports"))
    return {key: sorted(names) for key, names in found.items()}

