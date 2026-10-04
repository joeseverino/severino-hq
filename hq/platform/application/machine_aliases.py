"""Which records name the same machine.

Connections, located origins and tailnet devices, folded onto the one declared
name they belong to.
"""

from __future__ import annotations

from hq.domains.control_plane.models import ProviderConnection

from .locate import Machines, points_at_host
from .tailnet_presence import Presence


def _connection_aliases(
    index: Machines, connections: tuple[ProviderConnection, ...]
) -> dict[str, str]:
    aliases = {}
    for connection in connections:
        if not points_at_host(connection.endpoint):
            continue
        owner = index.at(connection.endpoint)
        if owner and owner != connection.connection_ref:
            aliases[connection.connection_ref] = owner
    return aliases


def _located_aliases(index: Machines, located: dict[str, str]) -> dict[str, str]:
    aliases = {}
    for host, address in located.items():
        owner = index.at(address)
        if owner and owner != host:
            aliases[host] = owner
    return aliases


def _presence_address_aliases(
    index: Machines, present: dict[str, Presence]
) -> dict[str, str]:
    aliases = {}
    for name, presence in present.items():
        owner = next(
            (
                owner
                for address in presence.addresses
                if (owner := index.at(address)) and owner != name
            ),
            None,
        )
        if owner:
            aliases[name] = owner
    return aliases


def _presence_name_aliases(
    index: Machines, present: dict[str, Presence], claimed: dict[str, str]
) -> dict[str, str]:
    aliases = {}
    known = {_folded(existing): existing for existing in index.names}
    for name, presence in present.items():
        if name in claimed:
            continue
        owner = next(
            (
                owner
                for candidate in (name, presence.dns_name.partition(".")[0])
                if (owner := known.get(_folded(candidate))) and owner != name
            ),
            None,
        )
        if owner:
            aliases[name] = owner
    return aliases


def same_machine(
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

    The address is what they all agree on, so it is the identity, and the
    index is what turns an address into the one name kept for it, so the fold
    here and the machine a proxy is said to forward to are the same judgement.
    ``10.0.0.5`` and ``10.0.0.5:22`` are one machine.

    The name kept is the index's: a declaration first, then the name containers
    are reported under, then a credential's: most deliberate first.

    A machine whose address HQ has never recorded stays its own row. That is not
    a failure to detect a duplicate; it is HQ declining to assert two things are
    one when nothing it holds says so.
    """

    presence = present or {}
    aliases = _connection_aliases(index, connections)
    # A credential that opens a shell at an address something else already
    # claims is a second name for that machine, not a second machine.
    aliases.update(_located_aliases(index, located))
    # What a controller calls the host it found is not always that host's name
    # Portainer's own environment is called "local", and a controller
    # filling that in has only its own hostname to offer. Run the sweep from
    # somewhere else and every container lands on a machine that is not
    # running them.
    aliases.update(_presence_address_aliases(index, presence))
    # The tailnet is the first source that names machines HQ already knows
    # without using HQ's name for them.
    # And the ones no shared address folds, because the address was only ever
    # in the declaration and has now been left out of it. A tailnet device is
    # often the same machine under a name somebody typed into that laptop years
    # ago, but its MagicDNS name is a slug, and a slug is what HQ names
    # machines with. `Sam's MacBook Pro` does not match `sams-laptop`;
    # `sams-laptop.example.ts.net` does.
    #
    # Only where an address did not already answer, so a recorded address still
    # decides. Where neither matches (a device whose owner named it something
    # unrelated to HQ's name for the machine) it stays its own row, which is
    # HQ declining to assert two things are one when nothing says so.
    aliases.update(_presence_name_aliases(index, presence, aliases))
    return aliases


def _folded(name: str) -> str:
    """A name with the punctuation two sources spell differently taken out.

    A tailnet device, a Portainer environment and a machine entry are written
    by three different people at three different times. Hyphens, apostrophes,
    spaces and case are where they disagree; the letters are where they do not.
    """

    return "".join(char for char in str(name or "").lower() if char.isalnum())
