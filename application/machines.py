"""The machines HQ knows about, and everything that ties to one.

A machine is named in a container's record, in a proxy's forwarding address, in
what a Portainer reports it reaches and in a certificate's install list. This is
where those meet, so "what is on this machine" is one page rather than four read
in sequence.

Nothing here is declared. A machine exists because something reported it: a
credential that reaches it, a container running on it, a service served from it,
or the topology naming it. That is the whole membership rule, which is why
adding a VPS is registering it somewhere rather than entering it here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse

from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from control_plane.providers import PROVIDERS, normalized_hostname

from .services import CONTAINER_KIND, Running, _topology, container_watchers


@dataclass(frozen=True)
class Machine:
    """One machine, and everything HQ can say about it without being told."""

    name: str
    role: str = ""
    # How HQ gets to it, as connection refs. More than one is normal: a machine
    # can be an SSH transport and a Portainer environment at once, and which is
    # in play depends on what is being asked of it.
    reached_by: tuple[str, ...] = ()
    # Other names this same machine is known by. Two credentials naming one
    # machine differently is normal, and keeping them apart splits its facts
    # across two rows.
    aliases: tuple[str, ...] = ()
    address: str = ""
    containers: tuple[Running, ...] = ()
    hostnames: tuple[str, ...] = ()
    resources: tuple[str, ...] = field(default_factory=tuple)

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
    roles = _roles()
    services = _services_by_host(connections, containers)
    resources = _resources_by_host()
    names = (
        set(containers)
        | set(reached)
        | set(services)
        | set(resources)
        | {name for name in roles if name in reached or name in containers}
    )
    answered = {
        connection.connection_ref
        for connection in connections
        if connection.reachable
    }
    aliases, addresses = _same_machine(containers, connections)
    canonical = sorted(names - set(aliases))
    return tuple(
        Machine(
            name=name,
            role=roles.get(name, ""),
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
            address=addresses.get(name, "") or _address(name, connections),
            containers=tuple(containers.get(name, ())),
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


def _same_machine(
    containers: dict[str, list[Running]],
    connections: tuple[ProviderConnection, ...],
) -> tuple[dict[str, str], dict[str, str]]:
    """Names that are one machine, and where that machine is.

    Two credentials name one machine differently and neither is wrong: a
    Portainer calls a VPS by its environment name, a 1Password SSH item calls it
    whatever the operator called it. Kept apart, one machine is two rows with
    half its facts each.

    The address is what both agree on, so it is the identity. The name kept is
    the one the containers were reported under, because that is the name every
    declaration's ``host`` field already uses.
    """

    located = {
        host: found[0].host_address
        for host, found in containers.items()
        if found and found[0].host_address
    }
    aliases: dict[str, str] = {}
    for connection in connections:
        endpoint = connection.endpoint
        if not endpoint or "://" in endpoint:
            continue
        address = endpoint.rpartition(":")[0] or endpoint
        for host, known in located.items():
            if known == address and connection.connection_ref != host:
                aliases[connection.connection_ref] = host
    return aliases, located


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
        if connection.endpoint and "://" not in connection.endpoint:
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
        endpoint = connection.endpoint
        if endpoint and "://" not in endpoint:
            return endpoint
    return ""


def _roles() -> dict[str, str]:
    return {
        str(host["id"]): str(host.get("role", ""))
        for host in (_topology() or {}).get("hosts", ())
        if host.get("id")
    }


def _services_by_host(
    connections: tuple[ProviderConnection, ...],
    containers: dict[str, list[Running]],
) -> dict[str, set[str]]:
    """Which names are served from which machine.

    Read straight off the declarations that answer "and then what serves it".
    A board of machines needs one field from each service, and assembling every
    service in full to get it is a query per row and then some.

    The address is resolved against what already names machines here -- a
    container reporting where it runs, a credential pointing somewhere, the
    topology -- so this adds no query of its own.
    """

    located = _by_address(connections, containers)
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
        address, separator, _ = origin.rpartition(":")
        host = located.get(address if separator else origin, "")
        if not host:
            continue
        for name in names:
            hostname = normalized_hostname(name)
            if hostname:
                found.setdefault(host, set()).add(hostname)
    return found


def _by_address(
    connections: tuple[ProviderConnection, ...],
    containers: dict[str, list[Running]],
) -> dict[str, str]:
    """Every way of naming a machine, pointing at the one name kept for it."""

    located: dict[str, str] = {}
    for host, found in containers.items():
        located[host] = host
        if found and found[0].host_address:
            located[found[0].host_address] = host
    for connection in connections:
        endpoint = connection.endpoint
        if not endpoint or "://" in endpoint:
            continue
        address = endpoint.rpartition(":")[0] or endpoint
        located.setdefault(address, connection.connection_ref)
        located.setdefault(connection.connection_ref, connection.connection_ref)
    for host in (_topology() or {}).get("hosts", ()):
        identifier = str(host.get("id", ""))
        if not identifier:
            continue
        for key in ("id", "lan_ip", "ts_ip", "public_ip"):
            value = str(host.get(key, "") or "")
            if value:
                located.setdefault(value, located.get(identifier, identifier))
    return located


def _resources_by_host() -> dict[str, set[str]]:
    """Declarations that name a machine in their own spec.

    Read through the providers rather than by looking for a ``host`` key, so a
    provider that starts naming machines joins this by having the field and not
    by anything here learning about it.
    """

    found: dict[str, set[str]] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or "host" not in provider.spec_type.model_fields:
            continue
        host = str(resource.spec.get("host", "")).strip()
        if host:
            found.setdefault(host, set()).add(resource.key)
    return found
