"""Which machine an address belongs to, answered in one place.

A proxy's forwarding address, the machine board, a form and the connection
panel all ask "what is at this address", and there is one true answer for a
given set of evidence, so it is computed here and the surfaces differ only in
the evidence they hand it. Names and addresses are separate namespaces
(``Machines``), and every endpoint is read by one parser (``split_endpoint``).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from hq.domains.control_plane.names import normalized_hostname
from hq.platform.core.network import split_host_port
lazy from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
lazy from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
lazy from hq.domains.control_plane.providers import PROVIDERS

lazy from .connections import connection_rows
lazy from .facts import snapshots_of
lazy from .infrastructure import declared_machines


def split_endpoint(value: Any) -> tuple[str, str]:
    """An endpoint as ``(host, port)``, whichever shape it arrived in.

    A URL, a ``host:port``, a bracketed IPv6 endpoint and a bare address all
    reach HQ as "where this is". Either half may be empty, and a missing port
    is load-bearing: it is how HQ tells a DNS record pointing elsewhere on the
    internet from an ingress forwarding inside the network.
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
    # Never ``rpartition(":")``, which splits ``2001:db8::1`` into
    # ``2001:db8:`` and ``1``: ``core.network`` parses endpoints correctly for
    # the trusted-proxy gate, and every endpoint here goes through it.
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

    The distinction is what tells a connection that opens a shell somewhere
    (which *is* a machine) from a connection that talks to a service running
    on one. Stated once so every caller draws it the same way.
    """

    text = str(endpoint or "").strip()
    return bool(text) and "://" not in text


@dataclass(frozen=True, slots=True)
class Machines:
    """Every way HQ has of naming a machine, with the two kinds kept apart.

    ``by_name`` is HQ's own vocabulary (what a declaration, a sweep or a shell
    credential calls a machine); ``by_address`` is where the network reaches
    them. One dictionary would let a machine *named* ``10.0.0.5`` and another
    *answering at* it overwrite each other silently, by query order, so a
    caller says which kind of thing it holds.
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

        The address namespace only. A caller holding an address (a tailnet
        record, a forwarding target, a connection endpoint) must not match a
        machine because its *name* happens to be that string.
        """

        return self.by_address.get(host_of(endpoint), "")

    def resolve(self, endpoint: Any) -> str:
        """The machine an origin points at, whether it named one or addressed one.

        An origin is genuinely either: a stack declares the machine it runs on
        by name, and a proxy forwards to an address. Empty when neither knows
        it, which is the honest answer and leaves the raw address on the page.
        """

        text = str(endpoint or "").strip()
        if not text:
            return ""
        # The name first: an exact hit in HQ's own vocabulary is somebody's
        # deliberate act, while an address hit is an inference from a
        # declaration made elsewhere.
        return self.named(text) or self.named(host_of(text)) or self.at(text)

    def address_for(self, name: Any) -> str:
        """Where the network reaches the machine called this.

        The inverse question, and the same index answers it, which is the
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

    Evidence is a parameter because the surfaces differ in what they are
    entitled to use: the connection panel, on every page, resolves against the
    declarations it already read and spends nothing, while the machine board
    resolves against everything. Precedence is declaration, then what a sweep
    found, then what a credential points at: most deliberate first.
    """

    by_name: dict[str, str] = {}
    by_address: dict[str, str] = {}
    addresses: dict[str, list[str]] = {}

    # The first record of an address keeps it, so a machine HQ was told about
    # owns its address even when a container sweep and an SSH credential also
    # mention it, which keeps one machine from becoming three rows.
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
        ref = connection.connection_ref.strip()
        endpoint = connection.endpoint
        if not ref:
            continue
        if points_at_host(endpoint):
            # A credential that opens a shell somewhere is that somewhere, so
            # its ref is a machine name and its endpoint is that machine's
            # address.
            record(ref, endpoint)
            continue
        # A URL is a service running on a machine rather than the machine. The
        # address still reaches it (which is how a proxy forwarding at a
        # host HQ holds a credential for stops reading as "unknown host")
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

    found: dict[str, str] = {}
    for snapshot in snapshots_of(CONTAINER_KIND):
        for record in snapshot.records:
            host = str(record.get("host", "") or "")
            address = str(record.get("host_address", "") or "")
            if host and address:
                found.setdefault(host, address)
    return found


def _observed_record(provider, record) -> tuple[tuple, tuple]:
    """Read one provider record without letting a stale row end the sweep."""

    try:
        spec = provider.from_record(record)
        return tuple(provider.hostnames(spec)), tuple(provider.answers(spec))
    except KeyError, TypeError, ValueError:
        # The inventory sweep reports its own health. One unreadable row is
        # evidence about that row, not a reason to hide every healthy answer.
        return (), ()


def _record_observed_answers(found: dict[str, set[str]], names: tuple, addresses: tuple) -> None:
    """Normalize one provider's vocabulary into the shared answer index."""

    answered = tuple(str(address) for address in addresses if address)
    if not answered:
        return
    for name in names:
        label = normalized_hostname(name)
        if label:
            found.setdefault(label, set()).update(answered)


def observed_answers(snapshots=None) -> dict[str, tuple[str, ...]]:
    """Every address each hostname is *seen* answering at.

    Observed rather than declared: a record HQ has written down but not yet
    applied is an intention, and a certificate that counted intentions as
    evidence would report consumers no request has ever reached.
    """

    found: dict[str, set[str]] = {}
    # ``snapshots`` is every stored reading, from a caller that already holds
    # them; without it they are read here.
    for snapshot in ProviderInventory.objects.all() if snapshots is None else snapshots:
        # Driven by the provider registry rather than a list of DNS kinds: a
        # provider that reads its records back and says what a record answers
        # with resolves names, whatever it is called, so a DNS provider added
        # later is counted here too, the one place that decides who consumes a
        # certificate.
        provider = PROVIDERS.get(snapshot.kind)
        if provider is None or provider.from_record is None:
            continue
        if provider.answers is None or provider.hostnames is None:
            continue
        for record in snapshot.records:
            names, addresses = _observed_record(provider, record)
            _record_observed_answers(found, names, addresses)
    return {name: tuple(sorted(addresses)) for name, addresses in found.items()}


def names_by_connection() -> dict[str, tuple[str, ...]]:
    """The hostnames that land on the machine each connection reaches.

    Derived, never recorded: a sweep says which names answer at which address,
    a credential says which address it opens, and the index says which of those
    addresses are the same machine.
    """

    index = machines_index()
    answers = observed_answers()
    # Both sides are placed through the one index rather than compared as
    # strings, so a machine reached at one of its addresses and named at
    # another is still one machine. An address the index cannot place stands in
    # for itself, which makes the comparison exact rather than absent.
    at_machine: dict[str, set[str]] = {}
    for name, addresses in answers.items():
        for address in addresses:
            placed = index.resolve(address) or host_of(address)
            if placed:
                at_machine.setdefault(placed, set()).add(name)

    def place(endpoint: Any) -> str:
        """Which machine a connection's endpoint opens onto."""

        host = host_of(endpoint)
        if not host:
            return ""
        # A name becomes an address first, through what HQ already sweeps.
        # Resolved directly, a connection addressed by name lands on its own
        # credential: the index files a URL endpoint under the connection's ref
        # (so a proxy forwarding there stops reading as an unknown host), which
        # is circular here and would make the connection and the names
        # answering at its host's address two machines.
        for address in answers.get(normalized_hostname(host), ()):
            placed = index.resolve(address) or host_of(address)
            if placed:
                return placed
        # An address the index cannot name stands in for itself, as on the
        # other side of this join; the name would match no address.
        return index.resolve(host) or host

    found: dict[str, tuple[str, ...]] = {}
    for connection in ProviderConnection.objects.all():
        ref = connection.connection_ref.strip()
        placed = place(connection.endpoint)
        if ref and placed:
            found[ref] = tuple(sorted(at_machine.get(placed, ())))
    return found


def machines_index(declared: Iterable[Mapping[str, Any]] | None = None) -> Machines:
    """The index over everything HQ holds, read from the database.

    ``declared`` is accepted already-read because most callers have it: the
    service catalog reads the declarations once for a whole page and would
    otherwise pay for them twice.
    """

    return index_of(
        declared=declared_machines() if declared is None else declared,
        hosts=container_hosts(),
        connections=connection_rows(),
    )
