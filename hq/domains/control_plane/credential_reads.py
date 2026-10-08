"""The read permissions an observer credential needs, per connection provider.

A registered reading states its own ``requires``. The reads below run through
a credential but are not registered readings yet; each entry leaves this list
when its read becomes one.

Cloudflare entries are ``<permission group name> (<account|zone>)``. Tailscale
entries are bare scope names.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .observations import OBSERVATIONS

# What the zone sweep's registrar read needs: expiry and auto-renew.
REGISTRAR_READ = "Registrar: Domains Read (account)"

# Reads an observer credential needs that neither a registered reading nor a
# resource kind's declared parts state: what the sweep asks for on the way to
# them. Everything else is derived from those declarations.
UNREGISTERED_READS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "cloudflare_api": (
            ("Account Settings Read (account)", "The account and its analytics sites"),
            ("Account Analytics Read (account)", "Site analytics"),
        ),
        "tailscale": (
            ("devices:core:read", "Tailnet devices"),
            ("devices:routes:read", "Advertised and approved routes"),
            ("policy_file:read", "Tailnet policy"),
        ),
    }
)


def observer_permissions(provider: str) -> tuple[str, ...]:
    """Every read permission this provider's observer credential needs, sorted.

    What its readings require, what the parts of resource kinds read through it
    require, and the reads neither declares.
    """

    from .providers import PROVIDERS

    found = {
        name
        for spec in OBSERVATIONS.values()
        if spec.provider == provider
        for name in spec.requires
    }
    found.update(
        name
        for spec in PROVIDERS.values()
        for part in spec.parts
        # A part names its credential when it is not the kind's own.
        if part.provider == provider
        or (not part.provider and provider in spec.connection_providers)
        for name in part.requires
    )
    found.update(name for name, _ in UNREGISTERED_READS.get(provider, ()))
    return tuple(sorted(found))


@dataclass(frozen=True)
class Minter:
    """How an observer credential for one provider is minted and stored.

    ``stores`` pairs each ``--store`` flag with the projection variable whose
    field it fills, the secret first. ``bootstrap`` pairs each environment
    variable the script reads with the projection variable whose field on the
    bootstrap item holds it, so a bootstrap item is shaped like the connection.
    """

    script: str
    stores: tuple[tuple[str, str], ...]
    bootstrap: tuple[tuple[str, str], ...]
    # Where an operator creates the credential by hand instead.
    by_hand: str
    # The flag naming the account, when the credential is minted for one.
    account_flag: str = ""


MINTERS: Mapping[str, Minter] = MappingProxyType(
    {
        "cloudflare_api": Minter(
            script="scripts/mint-cloudflare-token.sh",
            stores=(("--store", "API_TOKEN"),),
            bootstrap=(("CLOUDFLARE_BOOTSTRAP_TOKEN", "API_TOKEN"),),
            by_hand="Cloudflare dashboard, My Profile, API Tokens: a custom token",
            account_flag="--account",
        ),
        "tailscale": Minter(
            script="scripts/mint-tailscale-client.sh",
            stores=(("--store", "CLIENT_SECRET"), ("--store-id", "CLIENT_ID")),
            bootstrap=(
                ("TAILSCALE_BOOTSTRAP_CLIENT_ID", "CLIENT_ID"),
                ("TAILSCALE_BOOTSTRAP_CLIENT_SECRET", "CLIENT_SECRET"),
            ),
            by_hand="Tailscale admin console, Settings, Trust credentials: an OAuth client",
        ),
    }
)
