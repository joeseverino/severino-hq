"""Grant ports the tailnet policy opens where what HQ knows listens binds only loopback.

Read from the policy's grants and tests, the machines they name, the
containers HQ read on each, and where HQ's own image serves. A finding
raises it (``tailnet_findings``); this module only derives it.
"""

import json
import re
from dataclasses import dataclass

from . import connections
from .projection import projection_scope, read_once
from .tailnet import _by_name, _grant_machines, _is_loopback, _PortNamer, _service_ports, _ssh_ports, devices, policy
lazy from .containers import containers
lazy from .hq_self import LISTENS_AT, own_container

# A fact on the tailnet connection: a grant port only loopback listens on.
GRANT_REACHES_ONLY_LOOPBACK = "grant-reaches-only-loopback"


@dataclass(frozen=True, slots=True)
class LoopbackPort:
    """A port a grant opens where what HQ knows listens there binds only loopback.

    ``hq`` when it is HQ's own port on HQ's machine, else it is the ports of
    the containers publishing it there.
    """

    entry: str
    port: int
    # The protocol the entry names; "" for every one.
    proto: str
    hq: bool


@dataclass(frozen=True, slots=True)
class LoopbackGrant:
    """A grant, one machine it names, and the ports it opens there to loopback alone."""

    grant: dict
    machine: str
    ports: tuple[LoopbackPort, ...]
    # Each policy test that accepts one of those ports, with it moved to deny.
    tests: tuple[dict, ...]

    @classmethod
    def of(cls, fact: str) -> LoopbackGrant:
        """One read back from the fact the topology carries it in."""

        said = json.loads(fact)
        ports = tuple(LoopbackPort(**port) for port in said["ports"])
        return cls(said["grant"], said["machine"], ports, tuple(said["tests"]))

    @property
    def fragment(self) -> dict:
        """The grant without those ports, every other key as written; {} when none is left."""

        dropped = {port.entry for port in self.ports}
        kept = [entry for entry in self.grant.get("ip") or () if entry not in dropped]
        return {**self.grant, "ip": kept} if kept else {}


# A grant entry naming one port, with its protocol when it names one.
_ONE_PORT = re.compile(r"(?:(tcp|udp):)?([0-9]+)")


def loopback_grants() -> tuple[LoopbackGrant, ...]:
    """Each grant port on a machine where what HQ knows listens there is bound to loopback.

    HQ knows two listeners: its own, from where its image serves, and each
    container's, from Docker's port bindings. A port is said only where one
    of them binds it to loopback and nothing else HQ reads serves it there:
    no container publishing it on another address, no host-network container
    exposing it, no SSH or service connection on it. A machine whose
    containers were not read says nothing.
    """

    with projection_scope():
        return read_once("tailnet.loopback_grants", _loopback_grants)


def _loopback_grants() -> tuple[LoopbackGrant, ...]:
    found = policy()
    if not found.grants:
        return ()
    known = devices()
    by_name = _by_name(connections.machines_once())
    namer = _PortNamer(_ssh_ports(), _service_ports())
    held = containers()
    own = own_container()
    out = []
    for grant in found.grants:
        for machine in _grant_machines(grant.get("dst") or (), known, by_name):
            read = [item for item in held if item.machine.name == machine.name and item.runtime is not None]
            ports = _loopback_ports(grant, machine, read, own, namer) if read else ()
            if ports:
                written = _as_written(grant, found.written_grants)
                out.append(LoopbackGrant(written, machine.name, ports, _moved_tests(found.tests, written, ports)))
    return tuple(out)


def _loopback_ports(grant: dict, machine, read, own, namer: _PortNamer) -> tuple[LoopbackPort, ...]:
    found = (_loopback_port(str(entry), machine, read, own, namer) for entry in grant.get("ip") or ())
    return tuple(port for port in found if port is not None)


def _loopback_port(entry: str, machine, read, own, namer: _PortNamer) -> LoopbackPort | None:
    match = _ONE_PORT.fullmatch(entry)
    if match is None:
        return None
    proto, port = match.group(1) or "", int(match.group(2))
    if namer.opens(machine, port) or namer.service(machine, port):
        return None
    host, served = LISTENS_AT
    hq = bool(machine.runs_hq) and port == served and proto in ("", "tcp") and _is_loopback(host)
    others = [item.runtime for item in read if own is None or item.address != own.address]
    published = _published_on(others, proto, port)
    if published is None:
        return None
    return LoopbackPort(entry, port, proto, hq) if hq or published else None


def _published_on(runtimes, proto: str, port: int) -> bool | None:
    """Whether a container publishes the port to loopback alone; None when one may serve it elsewhere."""

    published = False
    for runtime in runtimes:
        if runtime.get("network_mode") == "host" and port in (runtime.get("exposed_ports") or ()):
            return None
        bound = [
            binding
            for binding in runtime.get("port_bindings") or ()
            if str(binding.get("host_port", "")) == str(port) and _same_protocol(proto, binding)
        ]
        if any(not _is_loopback(str(binding.get("host_ip", "") or "")) for binding in bound):
            return None
        published = published or bool(bound)
    return published


def _same_protocol(proto: str, binding) -> bool:
    """Whether a binding's ``80/tcp`` is the protocol a grant entry names; none names every one."""

    _, _, bound = str(binding.get("container_port", "")).partition("/")
    return not proto or (bound or "tcp") == proto


def _as_written(grant: dict, written: tuple[dict, ...]) -> dict:
    """The document's own grant for one the reading extracted, or that one when none matches."""

    def same(item: dict) -> bool:
        return all(
            sorted(map(str, item.get(key) or ())) == sorted(map(str, grant.get(key) or ()))
            for key in ("src", "dst", "ip")
        )

    return next((item for item in written if same(item)), grant)


def _moved_tests(tests, grant: dict, ports: tuple[LoopbackPort, ...]) -> tuple[dict, ...]:
    """Each test accepting one of ``ports`` on a destination the grant names, with it in deny instead.

    Tailscale rejects a policy file whose test fails, so a test denying the
    port keeps a later edit from opening it again.
    """

    names = {str(name) for name in grant.get("dst") or ()}
    moved = (_moved_test(test, names, ports) for test in tests)
    return tuple(test for test in moved if test)


def _moved_test(test: dict, names: set[str], ports: tuple[LoopbackPort, ...]) -> dict:
    """The test with the ports it accepts on ``names`` moved to deny; {} when it accepts none."""

    proto = str(test.get("proto", "") or "")
    wanted = {f"{name}:{port.port}" for name in names for port in ports if not proto or port.proto in ("", proto)}
    accept = [str(entry) for entry in test.get("accept") or ()]
    leaving = [entry for entry in accept if entry in wanted]
    if not leaving:
        return {}
    kept = {key: value for key, value in test.items() if key not in ("accept", "deny")}
    if staying := [entry for entry in accept if entry not in leaving]:
        kept["accept"] = staying
    kept["deny"] = [*(str(entry) for entry in test.get("deny") or ()), *leaving]
    return kept
