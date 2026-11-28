"""Each port the tailnet admits someone to on a machine, as one fact: who the
policy admits, where the port is bound there and what serves it.

Whether a port is open is derived here and nowhere else. The machine's page,
the policy page's port names, the loopback finding and the reachability
answer all read this ``Port``, so no two of them can disagree about one port.
"""

import re
from dataclasses import dataclass

from django.utils.text import get_text_list

from hq.config.networks import is_loopback
lazy from hq.config.serve import LISTENS_AT
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS

from .projection import projection_scope, read_once
lazy from . import connections
lazy from .containers import containers
lazy from .entity_links import provider_label
lazy from .hq_self import own_container, site_label
lazy from .locate import host_of, points_at_host, split_endpoint
lazy from .policy_links import PolicyNames
lazy from .tailnet import devices, policy

# Names for a port nothing HQ knows of on the machine serves.
WELL_KNOWN_PORTS = {
    22: "SSH",
    53: "DNS",
    80: "HTTP",
    443: "HTTPS",
    3389: "RDP",
    5432: "PostgreSQL",
}
# A grant entry naming one port, with its protocol when it names one.
_ONE_PORT = re.compile(r"(?:(tcp|udp|sctp):)?([0-9]+)")


@dataclass(frozen=True, slots=True)
class Binding:
    """An address a port is bound at on its machine; "" for every address, and
    for every protocol."""

    address: str
    proto: str = ""


@dataclass(frozen=True, slots=True)
class Port:
    """One port on one machine: who the policy admits, where it is bound, what serves it."""

    machine: str
    number: int
    # Who the policy admits, a policy alias as the machine it names.
    open_to: tuple[str, ...] = ()
    bound: tuple[Binding, ...] = ()
    # What serves it, by its own name: HQ, SSH, a container, a connection's service.
    serves: tuple[str, ...] = ()
    # Whether the machine's containers were read, without which what HQ knows
    # binds it there may not be all that does.
    read: bool = False

    def loopback_only(self, proto: str = "") -> bool:
        """Whether everything known to bind it, over ``proto`` ("" for any), binds loopback alone."""

        found = [binding for binding in self.bound if not proto or not binding.proto or binding.proto == proto]
        return self.read and bool(found) and all(is_loopback(binding.address) for binding in found)

    @property
    def open(self) -> bool:
        return bool(self.open_to) and not self.loopback_only()

    @property
    def label(self) -> str:
        return self.serves[0] if self.serves else WELL_KNOWN_PORTS.get(self.number, "")

    @property
    def bound_at(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(binding.address or "every address" for binding in self.bound))

    @property
    def held(self) -> str:
        """What serves it and where it is bound, as one phrase: "example-db is bound only to 127.0.0.1"."""

        return f"{self.label} is bound only to {get_text_list(list(self.bound_at), 'and')}"


def single_port(entry) -> tuple[str, int] | None:
    """``(protocol, number)`` for a grant entry naming one port, "" for any protocol; None otherwise."""

    match = _ONE_PORT.fullmatch(str(entry))
    return (match.group(1) or "", int(match.group(2))) if match else None


def tailnet_ports() -> dict[tuple[str, int], Port]:
    """Every port the policy admits someone to, by machine name and number."""

    with projection_scope():
        return read_once("tailnet.ports", _ports)


def ports_of(machine: str) -> tuple[Port, ...]:
    """The ports of one machine, in number order."""

    return tuple(port for (name, _), port in sorted(tailnet_ports().items()) if name == machine)


def device_ports(device: str) -> tuple[Port, ...]:
    """The ports of the machine whose tailnet device this is, in number order."""

    return ports_of(_machine_of(device))


def port_on(device: str, number: int) -> Port | None:
    """The port on the machine whose tailnet device this is, where the policy admits someone to it."""

    return tailnet_ports().get((_machine_of(device), number))


def _machine_of(device: str) -> str:
    return next(
        (item.name for item in connections.machines_once() if item.presence and item.presence.tailnet_name == device),
        "",
    )


def grant_machines(names) -> tuple:
    """The catalog's machines a grant's destinations name, through their tailnet devices."""

    known = devices()
    wanted = {
        device.name
        for device in known.values()
        if any(name == "*" or name in device.principals or name in device.addresses for name in names)
    }
    return tuple(
        item
        for item in connections.machines_once()
        if item.presence is not None and item.presence.tailnet_name in wanted
    )


def grant_ports(grants) -> tuple[dict, ...]:
    """Each grant with ``ports``: ``(entry, name)`` per ``ip`` entry, the name
    the label of that port on the machines the grant names."""

    with projection_scope():
        held = tailnet_ports()
        shown = []
        for grant in grants:
            targets = grant_machines(grant.get("dst") or ())
            ports = []
            for entry in grant.get("ip") or ():
                one = single_port(entry)
                labels = [held[key].label for item in targets if one and (key := (item.name, one[1])) in held]
                ports.append((entry, next(filter(None, labels), "") if one else ""))
            shown.append({**grant, "ports": tuple(sorted(ports, key=lambda port: port_order(port[0])))})
        return tuple(shown)


def port_order(entry) -> tuple[int, str]:
    """Numeric order for ``443``, ``tcp:443``; anything else after."""

    one = single_port(entry)
    return (one[1], str(entry)) if one else (1 << 16, str(entry))


def _ports() -> dict[tuple[str, int], Port]:
    found = policy()
    catalog = connections.machines_once()
    by_device = {item.presence.tailnet_name: item for item in catalog if item.presence is not None}
    admitted: dict[tuple[str, int], list[str]] = {}
    # The sweep's answer is Tailscale's own; a grant's word stands only for a
    # port the sweep did not ask about.
    for device in devices().values():
        if (machine := by_device.get(device.name)) is not None:
            for number, who in device.reach.items():
                admitted[(machine.name, number)] = list(who)
    swept = set(admitted)
    for grant in found.grants:
        for machine in grant_machines(grant.get("dst") or ()):
            for entry in grant.get("ip") or ():
                if (one := single_port(entry)) and (key := (machine.name, one[1])) not in swept:
                    admitted.setdefault(key, []).extend(str(name) for name in grant.get("src") or ())
    names = PolicyNames(hosts=found.hosts, machines=catalog)
    evidence = _Evidence()
    by_name = {item.name: item for item in catalog}
    return {
        (machine, number): Port(
            machine,
            number,
            open_to=tuple(dict.fromkeys(names.machine_for(name) or name for name in who)),
            **evidence.of(by_name[machine], number),
        )
        for (machine, number), who in admitted.items()
    }


class _Evidence:
    """What binds and serves a port on a machine, from one read of each source."""

    def __init__(self):
        own = own_container()
        self.held = [item for item in containers() if own is None or item.address != own.address]
        rows = connections.connection_rows()
        self.shells = [row for row in rows if points_at_host(row.endpoint)]
        self.services = [row for row in rows if row.provider in CONNECTION_KINDS and not points_at_host(row.endpoint)]

    def of(self, machine, number: int) -> dict:
        bound: list[Binding] = []
        serves: list[str] = []
        refs = {*machine.reached_by, *machine.opened_by}
        host, served = LISTENS_AT
        if machine.runs_hq and number == served:
            bound.append(Binding(host, "tcp"))
            serves.append(site_label())
        for row in self.shells:
            address, port = split_endpoint(row.endpoint)
            if row.connection_ref in refs and port == str(number):
                bound.append(Binding(address))
                serves.append(provider_label("ssh"))
        here = [item for item in self.held if item.machine.name == machine.name]
        for item in sorted(here, key=lambda item: item.running.name):
            found = _container_bindings(item, number)
            bound.extend(found)
            if found:
                serves.append(item.running.name)
        names = {str(name).lower() for name in (machine.name, *machine.aliases)}
        addresses = {host_of(address) for address in machine.addresses}
        for row in self.services:
            address, port = split_endpoint(row.endpoint)
            if port != str(number):
                continue
            # A connection over loopback names what serves it, and says nothing
            # of where else it is bound.
            local = is_loopback(address) and str(row.controller_id).lower() in names
            if local or row.connection_ref in refs or address in addresses:
                serves.append(provider_label(row.provider))
                if not local:
                    bound.append(Binding(address))
        return {
            "bound": tuple(dict.fromkeys(bound)),
            "serves": tuple(dict.fromkeys(serves)),
            "read": any(item.runtime is not None for item in here),
        }


def _container_bindings(item, number: int) -> tuple[Binding, ...]:
    """Where a container binds the port on its machine; every address where it
    shares the host's network or its runtime was not read."""

    runtime = item.runtime
    if runtime is None:
        return (Binding(""),) if number in item.running.ports else ()
    if runtime.get("network_mode") == "host":
        return (Binding(""),) if number in (runtime.get("exposed_ports") or ()) else ()
    return tuple(
        Binding(
            str(binding.get("host_ip", "") or ""), str(binding.get("container_port", "")).partition("/")[2] or "tcp"
        )
        for binding in runtime.get("port_bindings") or ()
        if str(binding.get("host_port", "")) == str(number)
    )
