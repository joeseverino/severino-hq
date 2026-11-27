"""The machines HQ knows about, and everything that ties to one.

A machine is named in a container's record, a proxy's forwarding address, what
a Portainer reaches and a certificate's install list; this is where those meet,
so "what is on this machine" is one page rather than four. Observation first: a
machine exists because something reported it, and a declaration is for the
printer and the offline CA, which nothing will ever sweep.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND
from hq.domains.control_plane.provider_spec import origin_is_authoritative
from hq.domains.control_plane.providers import PROVIDERS, read_spec

from .container_state import ContainerCount
from .containers import Running, container_watchers
from .hq_self import hq_hostnames, hq_machine, scoped_served_at
from .integration_specs import ListRead, ResourceSpec, typed_keys
from .locate import Machines, host_of, index_of, observed_answers, points_at_host
from .machine_aliases import same_machine
from .resources import BoundedQuery
from .security import Capability
from .services import CONTAINER_KIND
from .tailnet import TAILNET_KIND
from .tailnet_presence import Presence, tailnet_presence
from .tone import Tone, Toned
lazy from . import connections, derived_reads, readings
lazy from .connection_lifecycle import WORKING
lazy from .connections import connection_rows, connection_standing
lazy from .entity_links import entity_link
lazy from .facts import Subject, snapshots_of
lazy from .infrastructure import declared_machines, enabled_resources
lazy from .machine_records import MachineRecord, MachineRow
lazy from .machine_roles import role_context, roles_of
lazy from .projection import read_once
lazy from .reach import network_of
lazy from .service_facets import connected_kinds
lazy from .whereabouts import locate, whereabouts


class MachineState(Toned):
    """Whether a machine is there, as its one state pill says it."""

    ONLINE = "online", Tone.GOOD
    # A laptop or phone that is off is away, not down.
    AWAY = "away", Tone.NEUTRAL
    OFFLINE = "offline", Tone.SERIOUS
    # A connection reaches it and did not answer: the credential may be what broke.
    NOT_ANSWERING = "not answering", Tone.SERIOUS
    UNMONITORED = "not monitored", Tone.NEUTRAL


def in_states(states: Iterable[str]) -> str:
    """How many machines are in each state, in the order the states first
    appear ("2 online, 1 away"): what the dashboard and the machines page say."""

    states = list(states)
    return ", ".join(f"{states.count(state)} {state}" for state in dict.fromkeys(states))


@dataclass(frozen=True, slots=True)
class Machine:
    """One machine, and everything HQ can say about it without being told."""

    name: str
    role: str = ""
    # The declaration that says what this machine is, when HQ holds one. A page
    # that prints a role and then says nothing declares the machine is
    # describing the same record twice and disowning it once.
    declaration: str = ""
    # How HQ gets to it, as connection refs. More than one is normal: a machine
    # can be an SSH transport and a Portainer environment at once, and which is
    # in play depends on what is being asked of it. Includes the tailnet
    # connection that read the machine's device.
    reached_by: tuple[str, ...] = ()
    # The subset whose credential opens something on the machine: a shell or a
    # Docker environment. What telemetry can be read through.
    opened_by: tuple[str, ...] = ()
    # Other names this same machine is known by. Two credentials naming one
    # machine differently is normal, and keeping them apart splits its facts
    # across two rows.
    aliases: tuple[str, ...] = ()
    address: str = ""
    # Every address HQ knows reaches this machine. A machine on a tailnet has
    # at least two, and which one is right depends on where the client is,
    # so a page that prints one of them is answering a question it was not
    # asked.
    addresses: tuple[str, ...] = ()
    containers: tuple[Running, ...] = ()
    # The tailnet device declaration this machine has, when it has one. A verb
    # offered here acts on that record, and its key is not the tailnet's name
    # for the device.
    route_approval_key: str = ""
    # Controller readings belong to the declaration for this machine. Keeping
    # them on the read model means the dashboard, machine page, and adapters all
    # project the same observation instead of each owning a cache.
    telemetry: dict[str, Any] = field(default_factory=dict)
    telemetry_observed_at: Any = None

    @property
    def on_show(self) -> tuple[Running, ...]:
        return tuple(item for item in self.containers if not item.hidden)

    @property
    def other_declarations(self) -> tuple[str, ...]:
        """Declarations on this machine that the container table does not show.

        Every watched container is already a row below, named and linked, so
        listing its key again in a summary card says the same thing twice and
        makes that card the tallest thing on the page. What is left over is
        worth a line: anything else that names this host.
        """

        watched = {item.watcher for item in self.containers if item.watcher}
        return tuple(key for key in self.resources if key not in watched)

    @property
    def folded(self) -> tuple[Running, ...]:
        """Watched exactly like the rest, just not what you came to look at."""

        return tuple(item for item in self.containers if item.hidden)

    hostnames: tuple[str, ...] = ()
    resources: tuple[str, ...] = field(default_factory=tuple)
    # Whether HQ itself runs here, and the names it answers at. Derived; see
    # ``application.hq_self``. Never a declaration.
    runs_hq: bool = False
    hq_hostnames: tuple[str, ...] = ()
    # What it does for the estate, derived; see ``application.machine_roles``.
    roles: tuple[Any, ...] = ()

    @property
    def public_addresses(self) -> tuple[str, ...]:
        """Public addresses the machine's tailnet client reports."""

        return self.presence.public_addresses if self.presence is not None else ()

    # What the tailnet says about it, where the tailnet knows it at all.
    presence: Presence | None = None

    @property
    def subject(self) -> Subject:
        """What a reading about this machine names it by: its names, its
        addresses and the tailnet lock key its device holds."""

        return Subject.of(
            hostnames=(self.name, *self.aliases),
            addresses=self.addresses,
            keys=(self.presence.lock_key,) if self.presence is not None else (),
        )

    @property
    def is_server(self) -> bool:
        """Whether HQ runs here or manages it: HQ itself, a credential that
        opens a shell or a Docker environment on it, or a declaration that
        runs on it. A person's own device is none of these."""

        return self.runs_hq or bool(self.opened_by) or bool(self.resources)

    @property
    def url(self) -> str:
        return self.link.url

    @property
    def link(self):
        return entity_link("machine", self.name)

    @property
    def containers_visible(self) -> bool:
        """Whether any connected credential reads containers, so "none" is a reading."""

        return bool(self.containers) or CONTAINER_KIND in connected_kinds()

    @property
    def container_count(self) -> ContainerCount:
        return ContainerCount.of(container.state for container in self.containers)

    # Whether any credential that reaches this answered on the last sweep. A
    # machine nothing can reach is not necessarily down (the credential may be
    # what broke) so this says what HQ knows rather than what is true, and the
    # page it feeds says which of the two it means.
    #
    # Passed in rather than looked up, because a board of these would otherwise
    # ask the same table once per row.
    reachable: bool = False
    # The subset of ``reached_by`` whose connection did not answer its last probe.
    unanswered: tuple[str, ...] = ()

    @property
    def state(self) -> MachineState:
        """The one state pill a machine shows.

        Presence decides when the tailnet reports the machine: a device that is
        offline reads as offline whatever a credential says. Without presence, a
        connection that answered means the machine is up; one that did not is
        said as that, since the credential may be what broke. Which connection
        sees it is the "Reached through" column's to say.
        """

        if self.presence is not None:
            if self.presence.online:
                return MachineState.ONLINE
            return MachineState.AWAY if self.presence.personal else MachineState.OFFLINE
        if self.reached_by:
            return MachineState.ONLINE if self.reachable else MachineState.NOT_ANSWERING
        return MachineState.UNMONITORED


def _own(address: str) -> bool:
    """Whether an address says where a machine is. A loopback address is
    every machine's name for itself, so it places none of them."""

    host = host_of(address)
    return network_of(host) != "loopback" and host.lower() != "localhost"


def machine_catalog(*, served_at: tuple[str, ...] | None = None) -> tuple[Machine, ...]:
    """Every machine anything has reported, with what ties to it.

    ``served_at`` is the address a request reached HQ on; left out, the
    projection's (see ``hq_self.serving``), else none.
    """

    connections = connection_rows()
    containers = _containers()
    opened = _reached(connections)
    declared = _declarations()
    present = tailnet_presence()
    reached = _with_tailnet(opened, present, connections)
    # One index over everything this page has already read, and the same one
    # every other surface resolves an address through. Built here rather than
    # queried again because the readings above are exactly its evidence.
    addresses = _host_addresses(containers)
    index = index_of(
        declared=[{"name": name, "addresses": entry.addresses} for name, entry in declared.items()]
        # The tailnet's own addresses, as evidence rather than as something
        # somebody has to retype: without them a tailnet address resolves to a
        # machine only when an operator has copied it into the declaration.
        #
        # After the declarations, so a declared name still wins the address it
        # claims and nothing about precedence moves.
        + [{"name": name, "addresses": presence.addresses} for name, presence in present.items()],
        hosts=addresses,
        connections=connections,
    )
    services = _services_by_host(index)
    resources, device_keys = _resources_by_host()
    # A declared machine counts on its own; declaring one is a deliberate act.
    names = set(containers) | set(reached) | set(services) | set(resources) | set(declared) | set(present)
    answered = {connection.connection_ref for connection in connections if connection_standing(connection) in WORKING}
    aliases = same_machine(index, addresses, connections, present)
    canonical = sorted(names - set(aliases))
    hq_names = hq_hostnames()
    hq_on = hq_machine(
        index,
        hq_names,
        observed_answers() if hq_names else {},
        served_at=scoped_served_at() if served_at is None else served_at,
        devices=present.values(),
    )
    hq_on = aliases.get(hq_on, hq_on)
    return _with_roles(
        Machine(
            name=name,
            role=declared.get(name, Declared()).role,
            declaration=declared.get(name, Declared()).key,
            # Presence follows the name the tailnet used, which is often not
            # the name HQ uses. A laptop is "Sam's MacBook Pro" there and
            # "mac" here, and it is one machine either way.
            presence=present.get(name)
            or next(
                (present[alias] for alias, target in aliases.items() if target == name and alias in present),
                None,
            ),
            # Keyed by the tailnet's name for the device, for the same reason
            # presence is: a declaration adopted from a sweep carries the name
            # the tailnet used, not the one HQ lists the machine under.
            route_approval_key=device_keys.get(name)
            or next(
                (device_keys[alias] for alias, target in aliases.items() if target == name and alias in device_keys),
                "",
            ),
            telemetry=declared.get(name, Declared()).telemetry,
            telemetry_observed_at=declared.get(name, Declared()).telemetry_observed_at,
            reachable=any(ref in answered for ref in _refs(reached, name, aliases)),
            unanswered=tuple(ref for ref in _refs(reached, name, aliases) if ref not in answered),
            reached_by=_refs(reached, name, aliases),
            opened_by=_refs(opened, name, aliases),
            aliases=tuple(sorted(alias for alias, target in aliases.items() if target == name)),
            address=(
                next(filter(_own, (addresses.get(name, ""),)), "")
                or _address(name, connections)
                # Told, rather than found. A machine nothing sweeps still has
                # an address (it is how a proxy forwarding there was matched
                # to it in the first place) and printing nothing while the
                # declaration right below says otherwise is the page arguing
                # with itself.
                or next(iter(declared.get(name, Declared()).addresses), "")
            ),
            addresses=tuple(
                dict.fromkeys(
                    [
                        address
                        for address in (
                            addresses.get(name, ""),
                            _address(name, connections),
                        )
                        if address and _own(address)
                    ]
                    + list(declared.get(name, Declared()).addresses)
                    + list(
                        next(
                            (
                                present[alias].addresses
                                for alias, target in aliases.items()
                                if target == name and alias in present
                            ),
                            present.get(name).addresses if present.get(name) else (),
                        )
                    )
                )
            ),
            # Gathered across aliases like everything else here. A machine known
            # by two names runs one set of containers.
            containers=tuple(containers.get(name, ()))
            + tuple(item for alias, target in aliases.items() if target == name for item in containers.get(alias, ())),
            hostnames=tuple(
                sorted(
                    set(services.get(name, ()))
                    | {hostname for alias in aliases if aliases[alias] == name for hostname in services.get(alias, ())}
                )
            ),
            resources=tuple(sorted(resources.get(name, ()))),
            runs_hq=name == hq_on,
            hq_hostnames=hq_names if name == hq_on else (),
        )
        for name in canonical
    )


def _with_roles(machines) -> tuple[Machine, ...]:
    context = role_context()
    return tuple(replace(item, roles=roles_of(item, context)) for item in machines)


def _host_addresses(containers: dict[str, list[Running]]) -> dict[str, str]:
    """Where the sweep says each name it filed containers under actually is."""

    return {host: found[0].host_address for host, found in containers.items() if found and found[0].host_address}


def machine(name: str, *, served_at: tuple[str, ...] | None = None) -> Machine | None:
    """A machine by its name, or by another name it is known as: a tailnet
    device name that is the same machine as a declared one, or the key it was
    declared under, for instance."""

    wanted = name.strip().lower()
    # Inside a projection, the catalog every section and the relation graph share.
    catalog = connections.machines_once() if served_at is None else machine_catalog(served_at=served_at)
    return next(
        (item for item in catalog if item.name.lower() == wanted),
        next(
            (
                item
                for item in catalog
                if wanted in {alias.lower() for alias in (*item.aliases, item.declaration) if alias}
            ),
            None,
        ),
    )


def _containers() -> dict[str, list[Running]]:
    watchers = container_watchers()
    found: dict[str, list[Running]] = {}
    for snapshot in snapshots_of(CONTAINER_KIND):
        for record in snapshot.records:
            host = str(record.get("host", ""))
            if host:
                found.setdefault(host, []).append(Running.of(record, snapshot.observed_at, watchers))
    return found


def _reached(connections: tuple[ProviderConnection, ...]) -> dict[str, set[str]]:
    """Which connections reach which machine.

    Two shapes, because there are two ways a credential names a machine. A
    Portainer reports the environments it holds, and each is a machine. An SSH
    transport *is* a machine: the connection's own name is what HQ calls the
    thing at the other end of it.
    """

    found: dict[str, set[str]] = {}
    for connection in connections:
        if _reaches_machines(connection.provider):
            for name in connection.reaches:
                found.setdefault(str(name), set()).add(connection.connection_ref)
        # A connection that opens a shell somewhere *is* that somewhere.
        # Recognized by pointing at a host and a port rather than at a URL,
        # which is what an ssh_transport projection produces and nothing else
        # does, so a machine HQ can log into is a machine whether or not
        # anything else ever mentions it.
        if points_at_host(connection.endpoint):
            found.setdefault(connection.connection_ref, set()).add(connection.connection_ref)
    return found


def _with_tailnet(
    opened: dict[str, set[str]],
    present: dict[str, Presence],
    connections: tuple[ProviderConnection, ...],
) -> dict[str, set[str]]:
    """``opened``, plus each tailnet device reached through the connection that read it.

    A tailnet credential's probe names no machines, but every device in its
    reading is on the tailnet it opens. The record's ``connection_ref`` names
    the connection when present; otherwise the tailnet connections of the
    controller that took the reading.
    """

    providers = set(PROVIDERS[TAILNET_KIND].connection_providers)
    by_controller: dict[str, set[str]] = {}
    for connection in connections:
        if connection.provider in providers:
            by_controller.setdefault(connection.controller_id, set()).add(connection.connection_ref)
    found = {name: set(refs) for name, refs in opened.items()}
    for name, presence in present.items():
        refs = (
            {presence.connection_ref} if presence.connection_ref else by_controller.get(presence.controller_id, set())
        )
        if refs:
            found.setdefault(name, set()).update(refs)
    return found


def _refs(reached: dict[str, set[str]], name: str, aliases: dict[str, str]) -> tuple[str, ...]:
    """The connections reaching a machine under its own name or any alias."""

    return tuple(
        sorted(
            set(reached.get(name, ()))
            | {ref for alias, target in aliases.items() if target == name for ref in reached.get(alias, ())}
        )
    )


def _reaches_machines(provider: str) -> bool:
    """Whether what this kind of connection reaches are machines.

    ``reaches`` is deliberately polymorphic (a Portainer reports the machines
    it holds and a DNS token reports the zones it may edit) and read the same
    way, a zone would appear on this page as though it were a server.
    """

    if not provider:
        return False
    # Told apart by what the providers behind the connection declare: one with
    # a ``host`` field is one whose things live on machines.
    return any(provider in spec.connection_providers and declares_host(kind) for kind, spec in PROVIDERS.items())


def _address(name: str, connections: tuple[ProviderConnection, ...]) -> str:
    """Where the machine is, when a credential pointing at it says so."""

    for connection in connections:
        if connection.connection_ref != name:
            continue
        if points_at_host(connection.endpoint):
            return connection.endpoint
    return ""


@dataclass(frozen=True, slots=True)
class Declared:
    """What HQ was told about a machine, as opposed to what it found."""

    role: str = ""
    key: str = ""
    addresses: tuple[str, ...] = ()
    telemetry: dict[str, Any] = field(default_factory=dict)
    telemetry_observed_at: Any = None


def _declarations() -> dict[str, Declared]:
    """Everything HQ was told, by machine name."""

    declared = [
        (resource.key, resource.spec)
        for resource in enabled_resources()
        if resource.kind == MACHINE_KIND and resource.spec.get("name")
    ]
    telemetry = readings.stored_many(readings.machine_telemetry(key) for key, _ in declared)
    result = {}
    for key, spec in declared:
        reading = telemetry.get(readings.machine_telemetry(key))
        result[str(spec["name"])] = Declared(
            role=str(spec.get("role", "")),
            key=key,
            addresses=tuple(spec.get("addresses") or ()),
            telemetry=dict(reading.value) if reading else {},
            telemetry_observed_at=reading.observed_at if reading else None,
        )
    return result


def _origin_and_names(provider: Any, spec: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return provider.origin(spec) if provider.origin else "", tuple(provider.hostnames(spec))


def _services_by_host(index: Machines) -> dict[str, set[str]]:
    """Which names are served from which machine.

    Read straight off the declarations that answer "and then what serves it": a
    board of machines needs one field from each service, and assembling every
    service in full to get it is a query per row and then some.
    """

    # Ranked exactly as the service catalog ranks them, through the one rule
    # both read. A name whose ingress declares an origin is served where the
    # ingress forwards; the record pointing at that ingress is not a second
    # answer to file it under a second machine.
    routed: dict[str, str] = {}
    resolved: dict[str, str] = {}
    for resource in enabled_resources():
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.origin is None or provider.hostnames is None:
            continue
        read = read_spec(resource.kind, resource.spec, partial(_origin_and_names, provider))
        if read is None:
            continue
        origin, names = read
        if not origin:
            continue
        rank = routed if origin_is_authoritative(provider) else resolved
        for name in names:
            hostname = normalized_hostname(name)
            if hostname:
                rank.setdefault(hostname, origin)

    # Resolved through the index every other surface uses, so the machine this
    # board files a name under and the one its own page names cannot disagree.
    found: dict[str, set[str]] = {}
    for hostname, origin in {**resolved, **routed}.items():
        host = index.resolve(origin)
        if host:
            found.setdefault(host, set()).add(hostname)
    return found


def declares_host(kind: str) -> bool:
    """Whether a declaration of this kind names the machine it lives on.

    Read from the provider's spec rather than by looking for a ``host`` key, so
    a provider that starts naming machines is counted by having the field.
    """

    provider = PROVIDERS.get(kind)
    return provider is not None and "host" in provider.spec_type.model_fields


def _resources_by_host() -> tuple[dict[str, set[str]], dict[str, str]]:
    """Declarations that name a machine (:func:`declares_host`), and the
    tailnet device keys, in one pass.

    A verb offered on a machine page needs the key of the declaration it acts
    on, which is not the tailnet's name for the device and must not be guessed
    from it; the keys ride along because this loop already reads every resource.
    """

    found: dict[str, set[str]] = {}
    devices: dict[str, str] = {}
    for resource in enabled_resources():
        if resource.kind == TAILNET_KIND:
            declared_name = str(resource.spec.get("name", "")).strip()
            if declared_name:
                devices[declared_name] = resource.key
        if not declares_host(resource.kind):
            continue
        host = str(resource.spec.get("host", "")).strip()
        if host:
            found.setdefault(host, set()).add(resource.key)
    return found, devices


# What a request for a name passes through on its way, by the facet of the
# record that sends it there: each is one more name a restart interrupts.
PASSES_THROUGH = {"dns": "resolves", "proxy": "routes"}


@dataclass(frozen=True, slots=True)
class Serving:
    """What one container answers for: each name it is the origin of
    (``names``), and each name a connection inside it holds the record of, by
    what it does for it (``PASSES_THROUGH``: a DNS server resolves its
    rewrites, a proxy routes its hosts)."""

    names: tuple[str, ...] = ()
    through: tuple[tuple[str, tuple[str, ...]], ...] = ()


def served_by() -> dict[tuple[str, str], Serving]:
    """``{(machine, container): Serving}``: which names each container answers for.

    Resolved the same way a service resolves its own origin, but without
    assembling every service to ask: the board builds facets, health and
    certificates for each name, and none of that answers this question. Once
    per projection, so a page listing every container asks it once.
    """

    return read_once("machines.served_by", _served_by)


def connections_inside() -> dict[tuple[str, str], tuple[ProviderConnection, ...]]:
    """``{(machine, container): connections}``: each reported connection whose
    endpoint is a container, placed the way every address HQ holds is placed
    (``whereabouts.locate``); a loopback endpoint is the controller's own machine."""

    def build() -> dict[tuple[str, str], tuple[ProviderConnection, ...]]:
        at = whereabouts()
        found: dict[tuple[str, str], dict[str, ProviderConnection]] = {}
        for row in connection_rows():
            origin = locate(row.endpoint, at=at, loopback_on=row.controller_id) if row.endpoint else None
            if origin is not None and origin.host and origin.container:
                found.setdefault((origin.host, origin.container), {}).setdefault(row.connection_ref, row)
        return {key: tuple(rows.values()) for key, rows in found.items()}

    return read_once("machines.connections_inside", build)


def _served_by() -> dict[tuple[str, str], Serving]:
    machines = declared_machines()
    at = whereabouts(machines)
    holders = {row.connection_ref: key for key, rows in connections_inside().items() for row in rows}
    names: dict[tuple[str, str], set[str]] = {}
    through: dict[tuple[str, str], dict[str, set[str]]] = {}
    # The shared read of every enabled declaration, not a query of its own.
    for resource in enabled_resources():
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.hostnames is None:
            continue
        read = read_spec(resource.kind, resource.spec, partial(_origin_and_names, provider))
        if read is None:
            continue
        origin, found = read
        hostnames = {name for name in map(normalized_hostname, found) if name}
        located = locate(origin, machines, at) if origin else None
        if located is not None and located.host and located.container:
            names.setdefault((located.host, located.container), set()).update(hostnames)
        holder = holders.get(_held_through(resource.spec, provider))
        if holder is not None and provider.facet in PASSES_THROUGH:
            through.setdefault(holder, {}).setdefault(PASSES_THROUGH[provider.facet], set()).update(hostnames)
    return {
        key: Serving(
            tuple(sorted(names.get(key, ()))),
            tuple((does, tuple(sorted(held))) for does, held in sorted(through.get(key, {}).items())),
        )
        for key in names.keys() | through.keys()
    }


def _held_through(spec: dict[str, Any], provider: Any) -> str:
    """The connection a declaration's record is held through: the one it
    names, else the one connection of its provider's kinds, else none."""

    named = str(spec.get("connection_ref", "")).strip()
    if named:
        return named
    refs = {row.connection_ref for row in connection_rows() if row.provider in provider.connection_providers}
    return next(iter(refs)) if len(refs) == 1 else ""


def container_context(host: str, name: str) -> dict[str, object]:
    """What else a declared container is tied to, and what it is doing.

    The services are the inverse of the runtime claim: a service page resolves
    its origin to a machine and a container, so the containers that answer for a
    name are exactly the ones some service resolved to. Asked the other way
    (by matching published ports) a container on the host network answers for
    nothing, because Docker reports no ports for one.
    """

    found = machine(host)
    running = next(
        (item for item in (found.containers if found else ()) if item.name == name),
        None,
    )
    serving = served_by().get((found.name if found else host, name), Serving())
    return {"machine": found, "running": running, "serves": serving.names}


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "machines",
            "Machines",
            "Every machine anything reported, its roles, addresses and what runs on it.",
            Capability.READ,
            listing=ListRead(derived_reads.list_machines, BoundedQuery, MachineRecord, fields=typed_keys(MachineRow)),
            detail_handler=derived_reads.get_machine,
            identifier="name",
            web_route="control_plane:machines",
            record_type=MachineRecord,
        ),
    )
