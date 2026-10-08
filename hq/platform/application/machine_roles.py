"""What a machine does for the rest of the estate, derived from its readings.

A role is a rule over what HQ already read: the tailnet device reading and the
tailnet policy. Each rule is declared once, below; the machine list, the
machine page and search all read ``roles_of``.
"""

from dataclasses import dataclass
from typing import Callable

from hq.domains.control_plane.observations.tailscale import EXIT_ROUTES

from .locate import host_of



@dataclass(frozen=True)
class RoleContext:
    """Tailnet-wide facts a rule compares a machine against."""

    nameservers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MachineRole:
    id: str
    label: str
    detail: str
    applies: Callable[[object, RoleContext], bool]


def _exit_node(machine, context: RoleContext) -> bool:
    presence = getattr(machine, "presence", None)
    if presence is None:
        return False
    # Offered and approved: the machine advertises both default routes and the
    # tailnet hands them out.
    routed = EXIT_ROUTES <= set(presence.advertised_routes) and EXIT_ROUTES <= set(
        presence.enabled_routes
    )
    return routed or (presence.offers_exit_node and presence.exit_node_approved)


def _tailnet_dns(machine, context: RoleContext) -> bool:
    addresses = {host_of(address) for address in getattr(machine, "addresses", ())}
    return bool(addresses & context.nameservers)


ROLES: tuple[MachineRole, ...] = (
    MachineRole(
        "exit-node",
        "Exit node",
        "Other tailnet devices can send their internet traffic through it.",
        _exit_node,
    ),
    MachineRole(
        "tailnet-dns",
        "Tailnet DNS",
        "Tailnet devices use it for DNS.",
        _tailnet_dns,
    ),
)


def role_context() -> RoleContext:
    """The tailnet-wide facts, from the stored policy reading."""

    from .tailnet import policy

    return RoleContext(
        nameservers=frozenset(
            host_of(str(server)) for server in policy().dns.get("dns") or () if server
        )
    )


def roles_of(machine, context: RoleContext) -> tuple[MachineRole, ...]:
    return tuple(role for role in ROLES if role.applies(machine, context))
