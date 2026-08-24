"""Which machine an address belongs to, answered in one place.

Four surfaces asked this question and four answered it differently. A proxy's
forwarding address resolved against declarations and connections; the machine
board resolved the same address against containers and connections but not
loopback; a form resolved it against declarations alone; the connection panel
intersected address sets. So one address named a machine on one page, named a
different machine on the next, and named nothing on the third -- and each
answer was defensible in isolation, which is what made the disagreement so hard
to see.

The disagreement is not a rendering problem. Every one of those surfaces is
asking "what is at this address", and there is exactly one true answer for a
given set of evidence. So the answer is computed once, here, and the surfaces
differ only in what evidence they hand it.

Two rules make that safe.

**Names and addresses are separate namespaces.** A machine's name and a
machine's address are different kinds of fact that happen to share a type, and
kept in one dictionary they overwrite each other: a machine named like an IP
answers for another machine's address, and a connection whose ref equals some
machine's address quietly takes ownership of it. Both are recorded, neither can
shadow the other, and a caller says which kind of thing it is holding.

**One parser.** ``https://host/``, ``host:port``, ``[::1]:8000`` and a bare
IPv6 address are all endpoints, and reading them with ``rpartition(":")`` --
which five call sites did -- splits ``2001:db8::1`` into ``2001:db8:`` and
``1``. The one place that has always got this right is ``core.network``, which
does it for the trusted-proxy gate; this reads endpoints through it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from core.network import split_host_port


def split_endpoint(value: Any) -> tuple[str, str]:
    """An endpoint as ``(host, port)``, whichever shape it arrived in.

    A URL, a ``host:port``, a bracketed IPv6 endpoint and a bare address are
    all things HQ is handed as "where this is", by proxies, daemons, container
    runtimes and people. Either half may be empty: a DNS answer carries no
    port, and that absence is load-bearing -- it is how HQ tells a record
    pointing somewhere else on the internet from an ingress forwarding inside
    the network.
    """

    text = str(value or "").strip()
    if not text:
        return "", ""
    if "://" in text:
        parsed = urlsplit(text)
        try:
            port = parsed.port
        except ValueError:
            # A malformed authority is not an endpoint. Reporting the host
            # without the port would be inventing half a fact.
            return "", ""
        return parsed.hostname or "", str(port) if port else ""
    return split_host_port(text)


def host_of(value: Any) -> str:
    """The address half of an endpoint, with any port and brackets removed."""

    return split_endpoint(value)[0]


def join_endpoint(host: str, port: str) -> str:
    """An address and a port, written the way something else can read back.

    IPv6 gets its brackets here or not at all: ``fd00::1:8000`` is a valid
    address in its own right, so an unbracketed join produces a string that
    parses cleanly and means something else entirely.
    """

    if not host:
        return ""
    if not port:
        return host
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def points_at_host(endpoint: Any) -> bool:
    """Whether this endpoint names a host rather than a URL.

    The distinction is what tells a connection that opens a shell somewhere --
    which *is* a machine -- from a connection that talks to a service running
    on one. Written out longhand at five call sites, it was five chances to
    write it slightly differently.
    """

    text = str(endpoint or "").strip()
    return bool(text) and "://" not in text


@dataclass(frozen=True)
class Machines:
    """Every way HQ has of naming a machine, with the two kinds kept apart.

    ``by_name`` is HQ's own vocabulary: what a declaration called a machine,
    what a sweep filed containers under, what a credential that opens a shell
    is called. ``by_address`` is where the network reaches those machines.

    They are separate because they collide. A machine may legitimately be
    *named* ``10.0.0.5`` while a different machine *answers at* ``10.0.0.5``,
    and a single dictionary keeps whichever was written last -- silently, and
    differently depending on query order.
    """

    by_name: Mapping[str, str]
    by_address: Mapping[str, str]
    addresses: Mapping[str, tuple[str, ...]]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.by_name.values()))

    def named(self, value: Any) -> str:
        """The machine called this, if HQ calls a machine this."""

        return self.by_name.get(str(value or "").strip(), "")

    def at(self, endpoint: Any) -> str:
        """The machine answering at this address, if HQ knows of one.

        The address namespace only. A caller holding an address -- a tailnet
        record, a forwarding target, a connection endpoint -- must not match a
        machine because its *name* happens to be that string.
        """

        return self.by_address.get(host_of(endpoint), "")

    def resolve(self, endpoint: Any) -> str:
        """The machine an origin points at, whether it named one or addressed one.

        An origin is genuinely either: a stack declares the machine it runs on
        by name, and a proxy forwards to an address. So both namespaces are
        consulted -- the name first, because a name is HQ's own vocabulary and
        an exact hit in it was somebody's deliberate act, while an address hit
        is an inference from a declaration made elsewhere.

        Empty when neither knows it, which is the honest answer and the one
        that leaves the raw address on the page.
        """

        text = str(endpoint or "").strip()
        if not text:
            return ""
        return self.named(text) or self.named(host_of(text)) or self.at(text)

    def address_for(self, name: Any) -> str:
        """Where the network reaches the machine called this.

        The inverse question, and the same index answers it -- which is the
        point: a page that turns a name into an address and a page that turns
        an address into a name cannot disagree about the pair.
        """

        found = self.addresses.get(self.named(name), ())
        return found[0] if found else ""


def index_of(
    declared: Iterable[Mapping[str, Any]] = (),
    hosts: Mapping[str, str] | Iterable[tuple[str, str]] = (),
    connections: Iterable[Any] = (),
) -> Machines:
    """An index over exactly the evidence handed to it, and no queries.

    Evidence is a parameter because the surfaces genuinely differ in what they
    are entitled to use. The connection panel renders on every page and has
    already read the declarations, so it resolves against those and spends
    nothing. The machine board has read everything and resolves against
    everything. Passing the evidence in is what lets both use this one
    implementation without either paying for the other's queries.

    Precedence is declaration, then what a sweep found, then what a credential
    points at -- most deliberate first. A machine HQ was told about owns its
    address even when a container sweep and an SSH credential also mention it,
    which is what keeps one machine from becoming three rows.
    """

    by_name: dict[str, str] = {}
    by_address: dict[str, str] = {}
    addresses: dict[str, list[str]] = {}

    def record(name: Any, address: Any = "") -> None:
        label = str(name or "").strip()
        if not label:
            return
        canonical = by_name.setdefault(label, label)
        found = host_of(address)
        if not found:
            return
        by_address.setdefault(found, canonical)
        known = addresses.setdefault(canonical, [])
        if found not in known:
            known.append(found)

    for machine in declared:
        name = str(machine.get("name", "") or "").strip()
        if not name:
            continue
        record(name)
        for address in machine.get("addresses") or ():
            record(name, address)
    for host, address in dict(hosts).items():
        record(host, address)
    for connection in connections:
        ref = str(getattr(connection, "connection_ref", "") or "").strip()
        endpoint = getattr(connection, "endpoint", "") or ""
        if not ref:
            continue
        if points_at_host(endpoint):
            # A credential that opens a shell somewhere is that somewhere, so
            # its ref is a machine name and its endpoint is that machine's
            # address.
            record(ref, endpoint)
            continue
        # A URL is a service running on a machine rather than the machine. The
        # address still reaches it -- which is how a proxy forwarding at a
        # host HQ holds a credential for stops reading as "unknown host" --
        # but the ref is the credential's name, not the machine's, so it never
        # enters the name namespace.
        address = host_of(endpoint)
        if address:
            by_address.setdefault(address, ref)
    return Machines(
        by_name=by_name,
        by_address=by_address,
        addresses={name: tuple(found) for name, found in addresses.items()},
    )


def container_hosts() -> dict[str, str]:
    """Where each name a sweep filed containers under actually is.

    The first record wins, because a sweep reports one host address per host
    and a later disagreement is the same fact repeated rather than a second
    machine.
    """

    from control_plane.models import ProviderInventory
    from control_plane.providers import CONTAINER_KIND

    found: dict[str, str] = {}
    for snapshot in ProviderInventory.objects.filter(kind=CONTAINER_KIND):
        for record in snapshot.records:
            host = str(record.get("host", "") or "")
            address = str(record.get("host_address", "") or "")
            if host and address:
                found.setdefault(host, address)
    return found


def machines_index(declared: Iterable[Mapping[str, Any]] | None = None) -> Machines:
    """The index over everything HQ holds, read from the database.

    ``declared`` is accepted already-read because most callers have it: the
    service catalogue reads the declarations once for a whole page and would
    otherwise pay for them twice.
    """

    from control_plane.models import ProviderConnection

    from .infrastructure import declared_machines

    return index_of(
        declared=declared_machines() if declared is None else declared,
        hosts=container_hosts(),
        connections=tuple(ProviderConnection.objects.all()),
    )
