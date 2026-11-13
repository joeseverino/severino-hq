"""What the internet can reach, and how bad that is.

Derived from the service paths HQ already walks (``application.paths``), and
nothing is polled for it, so a problem can be ranked by who can reach what it
is about. The levels, a thing's exposure and the urgency each sets are
docs/Derived-Facts.md#exposure.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.domains.control_plane.observations.host import PERIMETER_KIND

from .facts import Subject, readings
from .path_model import Route
from .projection import read_once
from .reach import LOCAL_NETWORK, PUBLIC, UNKNOWN as UNKNOWN_REACH, Reach, narrowed, network_of, reach_of
lazy from . import paths
lazy from .paths import path_to, routed_names

OPEN, GATED, PRIVATE, UNKNOWN, UNROUTED = "open", "gated", "private", "unknown", "unrouted"
# Worst first: the order a page lists them and the index a ranking compares.
# Unknown ranks next to open, because for all HQ knows it is open.
LEVELS = (OPEN, UNKNOWN, GATED, PRIVATE, UNROUTED)
# A private route names no audience here: it says its ``Reach``, the one
# wording of who is inside, so a page's header and its path never disagree.
LABELS = {
    OPEN: "Open to the internet",
    GATED: "Open to the internet, behind a login",
    UNKNOWN: "Not known who can reach it",
    UNROUTED: "Not reachable",
}
# A few words each, for a table column; the sentence says the rest.
SHORT = {OPEN: "Internet", GATED: "Internet, behind a login", UNKNOWN: "Unknown", UNROUTED: "Not reachable"}
# The tone a page draws a level in. Internet-facing is stated plainly, since a
# public site is meant to be; only what HQ could not read is a warning, and
# exposure raises urgency where it matters, on the finding.
TONES = {OPEN: "neutral", UNKNOWN: "attention", GATED: "neutral", PRIVATE: "neutral", UNROUTED: "neutral"}
# What a serious problem becomes at each exposure. Unknown keeps it serious.
_SERIOUS_AT = {
    OPEN: "serious",
    GATED: "attention",
    PRIVATE: "attention",
    UNKNOWN: "serious",
    UNROUTED: "neutral",
}
# The ports a machine's front door answers on: a request routed to the machine
# lands on whichever container publishes one of them.
FRONT_DOOR_PORTS = frozenset({80, 443})


@dataclass(frozen=True, slots=True)
class RouteExposure:
    """One route to one name, and who it lets in."""

    hostname: str
    level: str
    line: str
    via: str
    # Who the route lets in: the internet for open and gated, the tailnet or
    # the home network for private.
    reach: Reach = UNKNOWN_REACH
    # The readings that gate the name: "Behind Access: Example app".
    gates: tuple[str, ...] = ()
    # Gates in front of some paths only, which leave the name as it was.
    path_gates: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.reach.label if self.level == PRIVATE else LABELS[self.level]

    @property
    def short(self) -> str:
        return self.reach.label if self.level == PRIVATE else SHORT[self.level]

    @property
    def rank(self) -> tuple[int, int, str]:
        """Worst first: the level, then within it the wider audience."""

        return LEVELS.index(self.level), self.reach.rank, self.hostname


@dataclass(frozen=True, slots=True)
class Exposure:
    """The routes reaching one thing, worst first."""

    routes: tuple[RouteExposure, ...] = ()
    # False when HQ has not read everything that could route to it, so an
    # empty ``routes`` means "not known" rather than "nothing".
    known: bool = True

    @property
    def level(self) -> str:
        if self.routes:
            return self.routes[0].level
        return UNROUTED if self.known else UNKNOWN

    @property
    def label(self) -> str:
        worst = self.worst
        return worst.label if worst is not None else LABELS[self.level]

    @property
    def tone(self) -> str:
        return TONES[self.level]

    @property
    def short(self) -> str:
        worst = self.worst
        return worst.short if worst is not None else SHORT[self.level]

    @property
    def reach(self) -> Reach:
        """Who the worst route lets in; unknown when no route was walked."""

        worst = self.worst
        return worst.reach if worst is not None else UNKNOWN_REACH

    @property
    def worst(self) -> RouteExposure | None:
        return self.routes[0] if self.routes else None

    @property
    def sentence(self) -> str:
        """ "Open to the internet as shop.example.com", or why it is not."""

        worst = self.worst
        if worst is None:
            return self.label
        gate = f" ({'; '.join(worst.gates)})" if worst.gates else ""
        partial = f"; login only at {', '.join(worst.path_gates)}" if worst.path_gates else ""
        return f"{worst.label} as {worst.hostname}{gate}{partial}"


def worse(one: str, other: str) -> str:
    return one if LEVELS.index(one) <= LEVELS.index(other) else other


def status_at(status: str, level: str) -> str:
    """How urgent a problem of ``status`` is at exposure ``level``.

    Only exposure lowers it: a serious problem nothing routes to is
    information, never the other way round. An attention item stays attention
    wherever it is reachable, and is information where nothing reaches it.
    """

    if status == "serious":
        return _SERIOUS_AT[level]
    if status == "attention" and level == UNROUTED:
        return "neutral"
    return status


def gates_of(hostname: str) -> tuple[str, ...]:
    """The readings that admit only whom they allow in front of all of ``hostname``."""

    return tuple(dict.fromkeys(label for label, paths in _gates(hostname) if not paths))


def path_gates_of(hostname: str) -> tuple[str, ...]:
    """The paths of ``hostname`` a gate stands in front of, when none covers
    all of it: the name stays as open as it was, and these are the parts that
    are not."""

    return tuple(dict.fromkeys(path for _label, paths in _gates(hostname) for path in paths))


def _gates(hostname: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    subject = Subject.of(hostnames=(hostname,))
    return tuple(
        (f"{joined.relation}: {joined.title}".rstrip(": "), _gated_paths(joined.record, hostname))
        for joined in readings().about(subject)
        if joined.spec.restricts and joined.hostnames
    )


def _gated_paths(record, hostname: str) -> tuple[str, ...]:
    """The paths a gate covers on ``hostname``, or () when it covers all of it.

    A gate scoped as ``host/wp-admin*`` protects that path, not the name: the
    rest of the site answers anyone. Only a bare host, ``host/`` or ``host/*``
    stands in front of everything. A gate that states no scope (an access
    list on a proxy host) covers the host it is attached to.
    """

    scopes = [str(item) for item in (record.get("domain"), *(record.get("domains") or ())) if item]
    paths = []
    for scope in scopes:
        host, _, path = scope.partition("/")
        if normalized_hostname(host) != hostname:
            continue
        if path in ("", "*"):
            return ()
        paths.append(f"/{path}")
    return tuple(paths)


def public_name(route: Route) -> bool:
    """Whether the route starts at a record anyone on the internet can resolve."""

    first = route.hops[0] if route.hops else None
    kind = first.source.kind if first is not None and first.source is not None else ""
    provider = PROVIDERS.get(kind)
    return bool(provider is not None and provider.public_effect)


def route_reach(route: Route) -> Reach:
    """Who this route admits a request from: the internet, or the widest
    private network its answers are on, narrowed by any proxy on the way
    whose access list admits less. It never names a wider audience than the
    proxy admits."""

    found = _answer_reach(route)
    for hop in route.hops:
        found = narrowed(found, hop.admits)
    return found


def _answer_reach(route: Route) -> Reach:
    answers = tuple(hop.detail for hop in route.hops if hop.step == "network")
    networks = {network_of(answer) for answer in answers}
    through_edge = any(hop.step == "edge" for hop in route.hops)
    if "public" in networks or (through_edge and public_name(route)):
        reachable = True
    elif networks:
        reachable = False
    else:
        # A public name whose answer HQ could not place: the internet can
        # resolve it, so it is not called private on a guess.
        reachable = public_name(route)
    if reachable:
        return PUBLIC
    # A name only a private resolver answers, at nothing HQ could place: the
    # widest private audience, since the narrower one would be a guess.
    found = reach_of(answers)
    return LOCAL_NETWORK if found is UNKNOWN_REACH else found


def route_level(hostname: str, route: Route) -> str:
    """Open, gated or private: which networks this route admits a request from."""

    if route_reach(route) is not PUBLIC:
        return PRIVATE
    return GATED if gates_of(hostname) else OPEN


def exposure_of_name(hostname: str) -> Exposure:
    """Every route to ``hostname``, worst first."""

    name = normalized_hostname(hostname)
    return read_once(f"exposure.name:{name}", lambda: _exposure_of_name(name))


def _exposure_of_name(name: str) -> Exposure:
    found = []
    for route in path_to(name).routes:
        level = route_level(name, route)
        found.append(
            RouteExposure(
                hostname=name,
                level=level,
                line=route.line,
                via=route.via,
                reach=route_reach(route),
                gates=gates_of(name) if level == GATED else (),
                path_gates=path_gates_of(name) if level == OPEN else (),
            )
        )
    return Exposure(tuple(sorted(found, key=lambda item: item.rank)))


def exposure_of_names(hostnames: Iterable[str]) -> Exposure:
    """The worst-first routes over several names: what serves them all."""

    routes = [route for name in sorted(set(hostnames)) for route in exposure_of_name(name).routes]
    return Exposure(tuple(sorted(routes, key=lambda item: item.rank)))


def publicly_answering(addresses: Iterable[str]) -> frozenset[int]:
    """The ports a perimeter check saw answer from the internet on these addresses."""

    subject = Subject.of(addresses=tuple(address for address in addresses if address))
    return frozenset(
        int(port)
        for joined in readings().about(subject, kinds=(PERIMETER_KIND,))
        for port in joined.record.get("answered_publicly") or ()
        if str(port).isdigit()
    )


def exposure_of_container(item) -> Exposure:
    """What reaches one running container: the names routed to it, and any
    port it publishes that the perimeter check saw answer from the internet."""

    return read_once(f"exposure.container:{item.address}", lambda: _exposure_of_container(item))


def routed_containers() -> dict[tuple[str, str], frozenset[str]]:
    """``{(machine, container): names}``: every container a walked route ends at.

    From the paths themselves, so a route HQ only reads (a proxy host nobody
    declared) counts as much as one it declares.
    """

    return read_once("exposure.routed_containers", _routed_containers)


def _routed_containers() -> dict[tuple[str, str], frozenset[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for name in routed_names():
        for route in path_to(name).routes:
            machine = ""
            for hop in route.hops:
                if hop.step == "machine":
                    machine = hop.name
                elif hop.step == "container" and machine:
                    found.setdefault((machine, hop.name), set()).add(name)
                elif hop.step == "ingress" and machine:
                    # Through the machine's front door: "" stands for whichever
                    # container publishes it, which the route does not name.
                    found.setdefault((machine, ""), set()).add(name)
    return {place: frozenset(names) for place, names in found.items()}


def listening_ports(item) -> tuple[int, ...]:
    """The host ports a container answers on: what it publishes, or, on the
    host's own network, what its image exposes, since there it binds them
    directly."""

    if item.running.network_mode != "host":
        return item.running.ports
    return tuple(int(port) for port in (item.runtime or {}).get("exposed_ports") or ())


def front_door_names(item) -> frozenset[str]:
    """The names routed through this container's machine's ingress, when this
    container is the one publishing the front door: a proxy is as exposed as
    the worst route it takes in, though no route names it."""

    if not FRONT_DOOR_PORTS.intersection(listening_ports(item)):
        return frozenset()
    return routed_containers().get((item.machine.name, ""), frozenset())


def _exposure_of_container(item) -> Exposure:
    names = {
        *item.serves,
        *routed_containers().get((item.machine.name, item.running.name), ()),
        *front_door_names(item),
    }
    named = exposure_of_names(names)
    machine = item.machine
    answering = publicly_answering((*machine.addresses, machine.address))
    ports = sorted(port for port in item.running.ports if port in answering)
    direct = tuple(
        RouteExposure(
            hostname=f"{machine.name}:{port}",
            level=OPEN,
            line=f"Port {port} on {machine.name}",
            via="Published port",
            reach=PUBLIC,
        )
        for port in ports
    )
    # Nothing routes to it only when HQ read every routing kind and, for a
    # container publishing ports, checked which answer from the internet.
    checked = not item.running.ports or bool(_perimeter_read(machine))
    return Exposure(
        tuple(sorted((*direct, *named.routes), key=lambda route: route.rank)),
        known=paths.reads_every_route() and checked,
    )


def _perimeter_read(machine) -> bool:
    addresses = (*machine.addresses, machine.address)
    subject = Subject.of(addresses=tuple(address for address in addresses if address))
    return bool(readings().about(subject, kinds=(PERIMETER_KIND,)))
