"""The machines HQ knows about, and everything that ties to one.

A machine is named in a container's record, in a proxy's forwarding address, in
what a Portainer reports it reaches and in a certificate's install list. This is
where those meet, so "what is on this machine" is one page rather than four read
in sequence.

A machine exists because something reported it -- a credential that reaches it,
a container running on it, a service served from it -- or because HQ was told
about one directly. Observation first: registering a VPS somewhere is what puts
it here, and a declaration is for the printer and the offline CA, which nothing
will ever sweep and which are still part of the place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory

from .locate import Machines, index_of, points_at_host
from control_plane.providers import MACHINE_KIND, PROVIDERS, normalized_hostname

from .services import CONTAINER_KIND, Running, container_watchers


TAILNET_KIND = "tailscale.device"


@dataclass(frozen=True)
class Presence:
    """Whether a machine is up, said by the network rather than by a service.

    Every other thing HQ knows about a machine is really about something running
    on it, so a box that is switched off and a box whose credential expired look
    the same. This is the one reading that tells them apart, which is why it is
    kept separate from ``reachable`` rather than folded into it.
    """

    online: bool = False
    last_seen: str = ""
    key_expires: str = ""
    addresses: tuple[str, ...] = ()
    # What the tailnet calls it, which is rarely what HQ does. Worth showing on
    # the machine's own page: it is the name in the Tailscale console, in
    # MagicDNS, and in an ACL -- so an operator moving between HQ and any of
    # those needs the join stated rather than inferred.
    tailnet_name: str = ""
    dns_name: str = ""
    os: str = ""
    offers_exit_node: bool = False
    # Offered and approved are two facts, and only their agreement means the
    # route works. A route is advertised by the machine and must then be
    # approved in the coordination server; until it is, the machine goes on
    # reporting that it offers the route and nothing can use it.
    exit_node_approved: bool = False
    advertised_routes: tuple[str, ...] = ()
    enabled_routes: tuple[str, ...] = ()
    # Facts with no symptom until they matter. A device the tailnet has not
    # authorised reaches nothing; one carrying a lock error cannot be reached
    # by anything under tailnet lock; and a client left behind is how a fleet
    # acquires versions nobody chose.
    authorized: bool = True
    lock_error: str = ""
    update_available: bool = False
    client_version: str = ""
    # Tailscale SSH turns a device into something the policy can hand shells
    # out on. Shields-up means it accepts no inbound connection at all, which
    # from outside looks exactly like being broken. An external device belongs
    # to another tailnet and was shared into this one.
    ssh_enabled: bool = False
    blocks_incoming: bool = False
    external: bool = False
    # The peering itself, as the machine HQ runs on reports it. This reading is
    # taken from HQ's own daemon, so every device in it is a peer of HQ by
    # construction -- which HQ knew and never said. A key that has completed a
    # handshake, over a path that was negotiated, carrying counted bytes, is
    # the difference between a machine HQ has been told about and one it is
    # actually talking to.
    public_key: str = ""
    direct_endpoint: str = ""
    relay: str = ""
    last_handshake: str = ""
    active: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0
    tags: tuple[str, ...] = ()
    # Who the policy admits, per port. Already swept for the reachability
    # panel, and the same answer a machine's own page should be able to give
    # without anybody having to go and ask it.
    openings: tuple[tuple[int, tuple[str, ...]], ...] = ()
    observed_at: Any = None

    @property
    def peered(self) -> bool:
        """Whether HQ and this machine have actually completed a handshake.

        Not whether the tailnet lists it. This reading comes from the daemon on
        the machine HQ runs on, so a device appearing at all means HQ has it in
        its network map -- but a key in a map is a machine HQ *could* talk to.
        A handshake is one it has.
        """

        return bool(self.public_key and self.last_handshake)

    @property
    def handshake(self) -> str:
        """When the two keys last completed a handshake, phrased as HQ phrases
        every other elapsed time."""

        from .ui import elapsed

        return elapsed(self.last_handshake)

    @property
    def peer_path(self) -> str:
        """How the two are reaching each other, in the terms WireGuard uses.

        A direct path means the two daemons found a route through both NATs and
        traffic goes machine to machine. A relayed one means they could not, and
        Tailscale's DERP servers are carrying the encrypted packets -- still
        end-to-end encrypted, still slower, and worth knowing which.
        """

        if not self.peered:
            return ""
        if self.direct_endpoint:
            return "direct"
        return "relayed" if self.relay else "negotiating"

    @property
    def unapproved_routes(self) -> tuple[str, ...]:
        """Routes this machine offers that the tailnet has not approved.

        The silent failure this reading exists for. `tailscale up
        --advertise-routes` succeeds, the machine reports the route forever,
        and every other device simply never receives it -- so a subnet route or
        an exit node can be declared, believed, and dead, with nothing in the
        estate disagreeing.
        """

        return tuple(
            route for route in self.advertised_routes
            if route not in set(self.enabled_routes)
        )

    @property
    def key_expiry_days(self) -> int | None:
        """Days until the node key expires, or None when it does not.

        A device with expiry disabled has no expiry, which is not the same as
        an expiry far away: one is a decision and the other is a deadline.
        """

        if not self.key_expires:
            return None
        moment = parse_datetime(self.key_expires)
        if moment is None:
            return None
        return (moment - timezone.now()).days


@dataclass(frozen=True)
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
    # in play depends on what is being asked of it.
    reached_by: tuple[str, ...] = ()
    # Other names this same machine is known by. Two credentials naming one
    # machine differently is normal, and keeping them apart splits its facts
    # across two rows.
    aliases: tuple[str, ...] = ()
    address: str = ""
    # Every address HQ knows reaches this machine. A machine on a tailnet has
    # at least two, and which one is right depends on where the client is --
    # so a page that prints one of them is answering a question it was not
    # asked.
    addresses: tuple[str, ...] = ()
    containers: tuple[Running, ...] = ()
    # The tailnet device declaration this machine has, when it has one. A verb
    # offered here acts on that record, and its key is not the tailnet's name
    # for the device.
    route_approval_key: str = ""

    @property
    def on_show(self) -> tuple[Running, ...]:
        return tuple(item for item in self.containers if not item.hidden)

    @property
    def other_declarations(self) -> tuple[str, ...]:
        """Declarations on this machine that the container table does not show.

        Every watched container is already a row below, named and linked, so
        listing its key again in a summary card says the same thing twice and
        makes that card the tallest thing on the page. What is left over is
        worth a line: a stack, or anything else that names this host.
        """

        watched = {item.watcher for item in self.containers if item.watcher}
        return tuple(key for key in self.resources if key not in watched)

    @property
    def folded(self) -> tuple[Running, ...]:
        """Watched exactly like the rest, just not what you came to look at."""

        return tuple(item for item in self.containers if item.hidden)
    hostnames: tuple[str, ...] = ()
    resources: tuple[str, ...] = field(default_factory=tuple)
    # What the tailnet says about it, where the tailnet knows it at all.
    presence: "Presence | None" = None

    @property
    def url(self) -> str:
        return reverse("control_plane:machine", kwargs={"name": self.name})

    @property
    def running(self) -> int:
        return sum(1 for container in self.containers if container.healthy)

    # Whether any credential that reaches this answered on the last sweep. A
    # machine nothing can reach is not necessarily down -- the credential may be
    # what broke -- so this says what HQ knows rather than what is true, and the
    # page it feeds says which of the two it means.
    #
    # Passed in rather than looked up, because a board of these would otherwise
    # ask the same table once per row.
    reachable: bool = False


def machine_catalog() -> tuple[Machine, ...]:
    """Every machine anything has reported, with what ties to it."""

    connections = tuple(ProviderConnection.objects.all())
    containers = _containers()
    reached = _reached(connections)
    declared = _declarations()
    present = tailnet_presence()
    # One index over everything this page has already read, and the same one
    # every other surface resolves an address through. Built here rather than
    # queried again because the readings above are exactly its evidence.
    addresses = _host_addresses(containers)
    index = index_of(
        declared=[
            {"name": name, "addresses": entry.addresses}
            for name, entry in declared.items()
        ],
        hosts=addresses,
        connections=connections,
    )
    services = _services_by_host(index)
    resources, device_keys = _resources_by_host()
    # A declared machine counts on its own. It used to have to be reached or
    # running something as well, because the declarations came from a document
    # that named a printer and a phone as readily as a Docker host and nobody
    # had asked for either. Declaring one is a deliberate act in HQ now, and a
    # board that drops what it was just told about is answering a question
    # nobody asked.
    names = set(containers) | set(reached) | set(services) | set(resources) | set(declared) | set(present)
    answered = {
        connection.connection_ref
        for connection in connections
        if connection.reachable
    }
    aliases = _same_machine(index, addresses, connections, present)
    canonical = sorted(names - set(aliases))
    return tuple(
        Machine(
            name=name,
            role=declared.get(name, Declared()).role,
            declaration=declared.get(name, Declared()).key,
            # Presence follows the name the tailnet used, which is often not
            # the name HQ uses. A laptop is "Joseph's MacBook Pro" there and
            # "mac" here, and it is one machine either way.
            presence=present.get(name)
            or next(
                (
                    present[alias]
                    for alias, target in aliases.items()
                    if target == name and alias in present
                ),
                None,
            ),
            # Keyed by the tailnet's name for the device, for the same reason
            # presence is: a declaration adopted from a sweep carries the name
            # the tailnet used, not the one HQ lists the machine under.
            route_approval_key=device_keys.get(name)
            or next(
                (
                    device_keys[alias]
                    for alias, target in aliases.items()
                    if target == name and alias in device_keys
                ),
                "",
            ),
            reachable=any(
                ref in answered
                for ref in set(reached.get(name, ()))
                | {
                    alias_ref
                    for alias, target in aliases.items()
                    if target == name
                    for alias_ref in reached.get(alias, ())
                }
            ),
            reached_by=tuple(
                sorted(
                    set(reached.get(name, ()))
                    | {
                        ref
                        for alias in aliases
                        if aliases[alias] == name
                        for ref in reached.get(alias, ())
                    }
                )
            ),
            aliases=tuple(
                sorted(alias for alias, target in aliases.items() if target == name)
            ),
            address=(
                addresses.get(name, "")
                or _address(name, connections)
                # Told, rather than found. A machine nothing sweeps still has
                # an address -- it is how a proxy forwarding there was matched
                # to it in the first place -- and printing nothing while the
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
                        if address
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
            + tuple(
                item
                for alias, target in aliases.items()
                if target == name
                for item in containers.get(alias, ())
            ),
            hostnames=tuple(
                sorted(
                    set(services.get(name, ()))
                    | {
                        hostname
                        for alias in aliases
                        if aliases[alias] == name
                        for hostname in services.get(alias, ())
                    }
                )
            ),
            resources=tuple(sorted(resources.get(name, ()))),
        )
        for name in canonical
    )


def _host_addresses(containers: dict[str, list[Running]]) -> dict[str, str]:
    """Where the sweep says each name it filed containers under actually is."""

    return {
        host: found[0].host_address
        for host, found in containers.items()
        if found and found[0].host_address
    }


def _same_machine(
    index: Machines,
    located: dict[str, str],
    connections: tuple[ProviderConnection, ...],
    present: dict[str, Presence] | None = None,
) -> dict[str, str]:
    """Names that are one machine.

    Two things name one machine differently and neither is wrong: a Portainer
    calls a VPS by its environment name, a 1Password SSH item calls it whatever
    the operator called it, and a tailnet calls it whatever its owner typed into
    that laptop years ago. Kept apart, one machine is several rows with a
    fraction of its facts each.

    The address is what they all agree on, so it is the identity -- and the
    index is what turns an address into the one name kept for it, so the fold
    here and the machine a proxy is said to forward to are the same judgement.
    This used to compare address strings it had split itself, which meant a
    machine recorded once as ``10.0.0.5`` and once as ``10.0.0.5:22`` was two
    machines, and one recorded at a bracketed IPv6 endpoint was two more.

    The name kept is the index's: a declaration first, then the name containers
    are reported under, then a credential's -- most deliberate first.

    A machine whose address HQ has never recorded stays its own row. That is not
    a failure to detect a duplicate; it is HQ declining to assert two things are
    one when nothing it holds says so.
    """

    aliases: dict[str, str] = {}
    # A credential that opens a shell at an address something else already
    # claims is a second name for that machine, not a second machine. Compared
    # only against container sweeps before, which is why a declared machine and
    # the SSH credential reaching it sat side by side as two rows -- the
    # declaration holding the role and the address, the credential holding
    # everything served from it.
    for connection in connections:
        if not points_at_host(connection.endpoint):
            continue
        owner = index.at(connection.endpoint)
        if owner and owner != connection.connection_ref:
            aliases[connection.connection_ref] = owner
    # What a controller calls the host it found is not always that host's name
    # -- Portainer's own environment is called "local", and a controller
    # filling that in has only its own hostname to offer. Run the sweep from
    # somewhere else and every container lands on a machine that is not
    # running them.
    for host, address in located.items():
        owner = index.at(address)
        if owner and owner != host:
            aliases[host] = owner
    # The tailnet is the first source that names machines HQ already knows
    # without using HQ's name for them.
    for name, presence in (present or {}).items():
        for address in presence.addresses:
            owner = index.at(address)
            if owner and owner != name:
                aliases[name] = owner
                break
    return aliases


def machine(name: str) -> Machine | None:
    wanted = name.strip().lower()
    return next(
        (item for item in machine_catalog() if item.name.lower() == wanted), None
    )


def _containers() -> dict[str, list[Running]]:
    watchers = container_watchers()
    found: dict[str, list[Running]] = {}
    for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
        for record in snapshot.records:
            host = str(record.get("host", ""))
            if host:
                found.setdefault(host, []).append(
                    Running.of(record, snapshot.observed_at, watchers)
                )
    return found


def tailnet_presence() -> dict[str, Presence]:
    """Presence by machine name, as the tailnet last reported it."""

    found: dict[str, Presence] = {}
    # The same read the policy uses. Two kinds in one table, asked once.
    from .tailnet import snapshots

    for snapshot in snapshots()[TAILNET_KIND]:
        for record in snapshot.records:
            name = str(record.get("name", ""))
            if not name:
                continue
            found[name] = Presence(
                online=bool(record.get("online")),
                last_seen=str(record.get("last_seen", "")),
                key_expires=str(record.get("key_expires", "")),
                addresses=tuple(str(a) for a in record.get("addresses") or ()),
                tailnet_name=name,
                dns_name=str(record.get("dns_name", "")),
                os=str(record.get("os", "")),
                offers_exit_node=bool(record.get("offers_exit_node")),
                exit_node_approved=bool(record.get("exit_node_approved")),
                advertised_routes=tuple(
                    str(r) for r in record.get("advertised_routes") or ()
                ),
                enabled_routes=tuple(
                    str(r) for r in record.get("enabled_routes") or ()
                ),
                authorized=bool(record.get("authorized", True)),
                lock_error=str(record.get("lock_error", "")),
                update_available=bool(record.get("update_available")),
                client_version=str(record.get("client_version", "")),
                ssh_enabled=bool(record.get("ssh_enabled")),
                blocks_incoming=bool(record.get("blocks_incoming")),
                external=bool(record.get("external")),
                public_key=str(record.get("public_key", "")),
                direct_endpoint=str(record.get("direct_endpoint", "")),
                relay=str(record.get("relay", "")),
                last_handshake=str(record.get("last_handshake", "")),
                active=bool(record.get("active")),
                rx_bytes=int(record.get("rx_bytes") or 0),
                tx_bytes=int(record.get("tx_bytes") or 0),
                tags=tuple(str(tag) for tag in record.get("tags") or ()),
                openings=tuple(
                    (int(entry["port"]), tuple(entry.get("who") or ()))
                    for entry in record.get("reach") or ()
                    if str(entry.get("port", "")).isdigit()
                ),
                observed_at=snapshot.observed_at,
            )
    return found


def _reached(connections: tuple[ProviderConnection, ...]) -> dict[str, set[str]]:
    """Which connections reach which machine.

    Two shapes, because there are two ways a credential names a machine. A
    Portainer reports the environments it holds, and each is a machine. An SSH
    transport *is* a machine -- the connection's own name is what HQ calls the
    thing at the other end of it.
    """

    found: dict[str, set[str]] = {}
    for connection in connections:
        if _reaches_machines(connection.provider):
            for name in connection.reaches:
                found.setdefault(str(name), set()).add(connection.connection_ref)
        # A connection that opens a shell somewhere *is* that somewhere.
        # Recognised by pointing at a host and a port rather than at a URL,
        # which is what an ssh_transport projection produces and nothing else
        # does -- so a machine HQ can log into is a machine whether or not
        # anything else ever mentions it.
        if points_at_host(connection.endpoint):
            found.setdefault(connection.connection_ref, set()).add(
                connection.connection_ref
            )
    return found


def _reaches_machines(provider: str) -> bool:
    """Whether what this kind of connection reaches are machines.

    ``reaches`` is deliberately polymorphic -- a Portainer reports the machines
    it holds and a DNS token reports the zones it may edit -- and read the same
    way, four domains appeared on this page as though they were servers.

    Told apart by what the providers behind each connection actually declare: a
    provider that has a ``host`` field is one whose things live on machines.
    """

    if not provider:
        return False
    return any(
        provider in spec.connection_providers
        and "host" in spec.spec_type.model_fields
        for spec in PROVIDERS.values()
    )


def _address(name: str, connections: tuple[ProviderConnection, ...]) -> str:
    """Where the machine is, when a credential pointing at it says so."""

    for connection in connections:
        if connection.connection_ref != name:
            continue
        if points_at_host(connection.endpoint):
            return connection.endpoint
    return ""


@dataclass(frozen=True)
class Declared:
    """What HQ was told about a machine, as opposed to what it found."""

    role: str = ""
    key: str = ""
    addresses: tuple[str, ...] = ()


def _declarations() -> dict[str, Declared]:
    """Everything HQ was told, by machine name."""

    return {
        str(spec["name"]): Declared(
            role=str(spec.get("role", "")),
            key=key,
            addresses=tuple(spec.get("addresses") or ()),
        )
        for key, spec in ManagedResource.objects.filter(
            kind=MACHINE_KIND, enabled=True
        ).values_list("key", "spec")
        if spec.get("name")
    }


def _services_by_host(index: Machines) -> dict[str, set[str]]:
    """Which names are served from which machine.

    Read straight off the declarations that answer "and then what serves it".
    A board of machines needs one field from each service, and assembling every
    service in full to get it is a query per row and then some.

    The origin is resolved through the same index every other surface uses, so
    the machine this board files a name under and the machine that name's own
    page says it is served from cannot disagree. They did: this once kept its
    own map, in which a name and an address shared a namespace and a credential
    outranked a declaration, so a service appeared under the credential's name
    on the board and under the declared machine's on the service page.
    """

    found: dict[str, set[str]] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.origin is None or provider.hostnames is None:
            continue
        try:
            origin = provider.origin(resource.spec)
            names = tuple(provider.hostnames(resource.spec))
        except (KeyError, TypeError, ValueError):
            continue
        if not origin:
            continue
        host = index.resolve(origin)
        if not host:
            continue
        for name in names:
            hostname = normalized_hostname(name)
            if hostname:
                found.setdefault(host, set()).add(hostname)
    return found


def _resources_by_host() -> tuple[dict[str, set[str]], dict[str, str]]:
    """Declarations that name a machine, and the tailnet device keys, in one pass.

    Read through the providers rather than by looking for a ``host`` key, so a
    provider that starts naming machines joins this by having the field and not
    by anything here learning about it.

    The device keys ride along because the loop already reads every enabled
    resource, and a verb offered on a machine page needs the key of the
    declaration it acts on -- which is not the tailnet's name for the device
    and must not be guessed from it.
    """

    found: dict[str, set[str]] = {}
    devices: dict[str, str] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        if resource.kind == TAILNET_KIND:
            declared_name = str(resource.spec.get("name", "")).strip()
            if declared_name:
                devices[declared_name] = resource.key
        provider = PROVIDERS.get(resource.kind)
        if provider is None or "host" not in provider.spec_type.model_fields:
            continue
        host = str(resource.spec.get("host", "")).strip()
        if host:
            found.setdefault(host, set()).add(resource.key)
    return found, devices


def container_context(host: str, name: str) -> dict[str, object]:
    """What else a declared container is tied to, and what it is doing.

    The services are the inverse of the runtime claim: a service page resolves
    its origin to a machine and a container, so the containers that answer for a
    name are exactly the ones some service resolved to. Asked the other way --
    by matching published ports -- a container on the host network answers for
    nothing, because Docker reports no ports for one.
    """

    from control_plane.providers import normalized_hostname

    from .infrastructure import declared_machines
    from .services import _locate, whereabouts

    found = machine(host)
    running = next(
        (item for item in (found.containers if found else ()) if item.name == name),
        None,
    )
    # Resolved the same way a service resolves its own origin, but without
    # assembling every service to ask: the board builds facets, health and
    # certificates for each name, and none of that answers this question.
    machines = declared_machines()
    at = whereabouts(machines)
    wanted = found.name if found else host
    serves: set[str] = set()
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.origin is None or provider.hostnames is None:
            continue
        try:
            origin = provider.origin(resource.spec)
            names = tuple(provider.hostnames(resource.spec))
        except (KeyError, TypeError, ValueError):
            continue
        if not origin:
            continue
        located = _locate(origin, machines, at)
        if located.host == wanted and located.container == name:
            serves.update(normalized_hostname(item) for item in names)
    return {
        "machine": found,
        "running": running,
        "serves": tuple(sorted(item for item in serves if item)),
    }
