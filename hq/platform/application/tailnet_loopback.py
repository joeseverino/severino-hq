"""Grant ports the tailnet policy opens where every machine they name binds them only to loopback.

Read from the policy's grants and tests and each port's one fact
(``tailnet_ports``). A finding raises it (``tailnet_findings``); this module
only derives it.
"""

from dataclasses import dataclass

from .projection import projection_scope, read_once
from .tailnet import policy
from .tailnet_ports import Port, grant_machines, single_port, tailnet_ports

# A fact on the tailnet connection: a grant port only loopback listens on.
GRANT_REACHES_ONLY_LOOPBACK = "grant-reaches-only-loopback"


@dataclass(frozen=True, slots=True)
class LoopbackPort:
    """A grant entry, and the port it opens on each machine the grant names."""

    entry: str
    # The protocol the entry names; "" for every one.
    proto: str
    held: tuple[Port, ...]

    @property
    def number(self) -> int:
        return self.held[0].number


@dataclass(frozen=True, slots=True)
class LoopbackGrant:
    """A grant, as the document writes it, and the ports it opens to loopback alone."""

    grant: dict
    ports: tuple[LoopbackPort, ...]

    @property
    def fragment(self) -> dict:
        """The grant without those ports, every other key as written; {} when none is left."""

        dropped = {port.entry for port in self.ports}
        kept = [entry for entry in self.grant.get("ip") or () if entry not in dropped]
        return {**self.grant, "ip": kept} if kept else {}


def loopback_grants() -> tuple[LoopbackGrant, ...]:
    """Each grant with an entry that every machine it names binds only to loopback.

    Said only where each of those machines' containers were read, since what
    HQ knows binds a port is all that binds it only then.
    """

    with projection_scope():
        return read_once("tailnet.loopback_grants", _loopback_grants)


def _loopback_grants() -> tuple[LoopbackGrant, ...]:
    found = policy()
    held = tailnet_ports()
    out = []
    for grant in found.grants:
        machines = grant_machines(grant.get("dst") or ())
        ports = []
        for entry in grant.get("ip") or ():
            one = single_port(entry)
            on = tuple(held[key] for item in machines if one and (key := (item.name, one[1])) in held)
            if one and on and len(on) == len(machines) and all(port.loopback_only(one[0]) for port in on):
                ports.append(LoopbackPort(str(entry), one[0], on))
        if ports:
            out.append(LoopbackGrant(_as_written(grant, found.written_grants), tuple(ports)))
    return tuple(out)


def _as_written(grant: dict, written: tuple[dict, ...]) -> dict:
    """The document's own grant for one the reading extracted, or that one when none matches."""

    def same(item: dict) -> bool:
        return all(
            sorted(map(str, item.get(key) or ())) == sorted(map(str, grant.get(key) or ()))
            for key in ("src", "dst", "ip")
        )

    return next((item for item in written if same(item)), grant)


def moved_tests(grants: tuple[LoopbackGrant, ...]) -> tuple[tuple[dict, tuple[str, ...]], ...]:
    """Each policy test accepting one of the ports, with every one of them it
    accepts moved to deny, and those it moved.

    One per test whatever number of grants it crosses, so no replacement undoes
    another. Tailscale rejects a policy file whose test fails, so a test
    denying the port keeps a later edit from opening it again.
    """

    wanted = {
        (f"{name}:{port.number}", port.proto)
        for item in grants
        for name in item.grant.get("dst") or ()
        for port in item.ports
    }
    moved = (_moved_test(test, wanted) for test in policy().tests)
    return tuple(found for found in moved if found[1])


def _moved_test(test: dict, wanted: set[tuple[str, str]]) -> tuple[dict, tuple[str, ...]]:
    proto = str(test.get("proto", "") or "")
    accept = [str(entry) for entry in test.get("accept") or ()]
    leaving = tuple(
        entry
        for entry in accept
        if any(entry == target and (not proto or held in ("", proto)) for target, held in wanted)
    )
    if not leaving:
        return test, ()
    kept = {key: value for key, value in test.items() if key not in ("accept", "deny")}
    if staying := [entry for entry in accept if entry not in leaving]:
        kept["accept"] = staying
    kept["deny"] = [*(str(entry) for entry in test.get("deny") or ()), *leaving]
    return kept, leaving
