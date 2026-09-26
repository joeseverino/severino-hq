"""HQ as a service of its own.

Derived, never configured. The names are the site host and the concrete
allowed hosts. The machine is the one answering at an address HQ is reached at:
the address a request arrived on, what its names are seen resolving to, or the
host's own routing addresses. Each is matched against the addresses machines
carry, the same way the "this device" pill matches the caller's address, and
the host's own addresses also against the endpoints a tailnet device reports,
since a machine known only to the tailnet carries only tailnet addresses.
Loopback and link-local addresses name no machine.

Read-only. HQ's own service is not a declaration and nothing reconciles it.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from django.conf import settings

from control_plane.providers import normalized_hostname

from .locate import Machines, host_of



def site_label() -> str:
    """What HQ calls itself: the configured site name."""

    return str(getattr(settings, "SEVERINO_SITE_NAME", "") or "")


def __getattr__(name: str) -> str:
    # ``LABEL`` reads the setting when it is imported, for callers that import it.
    if name == "LABEL":
        return site_label()
    raise AttributeError(name)

# Names a deployment answers to that are not a place anyone reaches it by.
_NOT_A_SITE = frozenset({"localhost", "testserver"})

# The projection key holding the address a request reached HQ on.
_SERVED_AT = "hq.served_at"
_SERVED_PORT = "hq.served_port"


@dataclass(frozen=True)
class SelfService:
    hostnames: tuple[str, ...]
    machine: str = ""
    label: str = field(default_factory=site_label)

    @property
    def hostname(self) -> str:
        return self.hostnames[0] if self.hostnames else ""

    @property
    def machine_url(self) -> str:
        from .entity_links import entity_link

        return entity_link("machine", self.machine).url


def hq_hostnames() -> tuple[str, ...]:
    """The names HQ answers at: the site host first, then the allowed hosts."""

    found: list[str] = []
    for candidate in (
        getattr(settings, "SEVERINO_SITE_HOST", ""),
        *getattr(settings, "ALLOWED_HOSTS", ()),
    ):
        text = str(candidate or "").strip()
        # A wildcard or a leading dot is a pattern, not a name.
        if not text or "*" in text or text.startswith("."):
            continue
        name = normalized_hostname(text)
        if not name or name in _NOT_A_SITE or "." not in name or _is_address(name):
            continue
        if name not in found:
            found.append(name)
    return tuple(found)


def served_at(request: Any) -> tuple[str, ...]:
    """The address ``request`` reached HQ on, from the ASGI scope; () without one."""

    scope = getattr(request, "scope", None)
    server = scope.get("server") if isinstance(scope, Mapping) else None
    if not isinstance(server, (tuple, list)) or not server:
        return ()
    host = host_of(str(server[0] or ""))
    return (host,) if _matchable(host) else ()


def served_port(request: Any) -> int | None:
    """The port ``request`` reached HQ on, from the ASGI scope; None without one."""

    scope = getattr(request, "scope", None)
    server = scope.get("server") if isinstance(scope, Mapping) else None
    if not isinstance(server, (tuple, list)) or len(server) < 2:
        return None
    try:
        return int(server[1])
    except (TypeError, ValueError):
        return None


def serving(request: Any) -> dict[str, Any]:
    """A projection seed carrying the address and port ``request`` reached HQ on."""

    return {_SERVED_AT: served_at(request), _SERVED_PORT: served_port(request)}


def scoped_served_at() -> tuple[str, ...]:
    """The address the current projection was seeded with, or ()."""

    from .projection import read_once

    return read_once(_SERVED_AT, tuple)


def scoped_served_port() -> int | None:
    """The port the current projection was seeded with, or None."""

    from .projection import read_once

    return read_once(_SERVED_PORT, lambda: None)


@cache
def host_addresses() -> frozenset[str]:
    """The addresses the kernel routes from on this host, cached for the process.

    A connected UDP socket sends no packet and puts no DNS in the request path.
    """

    found = {"127.0.0.1", "::1"}
    for family, destination in (
        (socket.AF_INET, "192.0.2.1"),
        (socket.AF_INET6, "2001:db8::1"),
    ):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect((destination, 9))
                found.add(str(probe.getsockname()[0]).split("%", 1)[0])
        except OSError:
            continue
    return frozenset(found)


def own_addresses(served: Iterable[str] = ()) -> tuple[str, ...]:
    """The addresses on HQ's own host that can name a machine: where a request
    arrived, then where the host routes from. Loopback and link-local are left out."""

    found: list[str] = []
    for address in (*served, *sorted(host_addresses())):
        host = host_of(address)
        if _matchable(host) and host not in found:
            found.append(host)
    return tuple(found)


def hq_addresses(
    hostnames: tuple[str, ...],
    answers: Mapping[str, tuple[str, ...]],
    served: Iterable[str] = (),
) -> tuple[str, ...]:
    """Where HQ is reached: its own host's addresses, then what its names answer with."""

    found = list(own_addresses(served))
    for name in hostnames:
        for address in answers.get(name, ()):
            host = host_of(address)
            if host and host not in found and _matchable(host):
                found.append(host)
    return tuple(found)


def hq_machine(
    index: Machines,
    hostnames: tuple[str, ...],
    answers,
    *,
    served_at: Iterable[str] = (),
    devices: Iterable[Any] = (),
) -> str:
    """The machine HQ runs on, or "" when no address it holds is one HQ has.

    ``devices`` carry ``addresses`` and ``endpoints``, as tailnet devices do. A
    device reporting one of the host's own addresses as an endpoint names the
    machine its tailnet addresses belong to, when exactly one device does.
    """

    served = tuple(served_at)
    for address in hq_addresses(hostnames, answers, served):
        if owner := index.at(address):
            return owner
    mine = set(own_addresses(served))
    owners = {
        owner
        for device in devices
        if mine.intersection(host_of(endpoint) for endpoint in device.endpoints)
        for address in device.addresses
        if (owner := index.at(address))
    }
    return owners.pop() if len(owners) == 1 else ""


def hq_service(
    request: Any = None, *, catalog: Iterable[Any] | None = None
) -> SelfService | None:
    """HQ's own service, or None when it answers at no name worth listing.

    The machine is the machine catalogue's answer, so every page agrees.
    """

    from .machines import machine_catalog

    hostnames = hq_hostnames()
    if not hostnames:
        return None
    if catalog is None:
        catalog = machine_catalog(served_at=served_at(request) if request is not None else None)
    found = next((item for item in catalog if item.hq_hostnames), None)
    return SelfService(hostnames=hostnames, machine=found.name if found else "")


def _matchable(text: str) -> bool:
    """Whether an address can name a machine: an address, neither loopback nor link-local."""

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_link_local or address.is_unspecified)


def _is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
