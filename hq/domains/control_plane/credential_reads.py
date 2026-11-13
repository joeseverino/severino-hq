"""The read permissions an observer credential needs, per connection provider.

A registered reading states its own ``requires``; the reads below run through
a credential but are not registered readings yet, and each entry leaves this
list when its read becomes one. Each name is a member of its provider's table
in ``permissions``.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .observations import OBSERVATIONS
from .permissions import Cloudflare, Permission, Tailscale
lazy from .providers import PROVIDERS

# Reads an observer credential needs that neither a registered reading nor a
# resource kind's declared parts state: what the sweep asks for on the way to
# them. Everything else is derived from those declarations.
UNREGISTERED_READS: Mapping[str, tuple[tuple[Permission, str], ...]] = frozendict(
    {
        "cloudflare_api": (
            (Cloudflare.ACCOUNT_SETTINGS_READ, "The account and its analytics sites"),
            (Cloudflare.ACCOUNT_ANALYTICS_READ, "Site analytics"),
        ),
        "tailscale": (
            (Tailscale.DEVICES_CORE_READ, "Tailnet devices"),
            (Tailscale.DEVICES_ROUTES_READ, "Advertised and approved routes"),
            (Tailscale.POLICY_FILE_READ, "Tailnet policy"),
            # The policy read needs it beside devices:core:read, the scopes table says.
            (Tailscale.DEVICES_POSTURE_ATTRIBUTES_READ, "Tailnet policy"),
        ),
    }
)


def observer_permissions(provider: str) -> tuple[Permission, ...]:
    """Every read permission this provider's observer credential needs, sorted.

    What its readings require, what the parts of resource kinds read through it
    require, and the reads neither declares.
    """

    found = {name for spec in OBSERVATIONS.values() if spec.provider == provider for name in spec.requires}
    found.update(
        name
        # A part names its credential when it is not the kind's own.
        for spec in PROVIDERS.values()
        for part in spec.parts
        if part.provider == provider or (not part.provider and provider in spec.connection_providers)
        for name in part.requires
    )
    found.update(name for name, _ in UNREGISTERED_READS.get(provider, ()))
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class Minter:
    """How an observer credential for one provider is minted and stored.

    ``task`` is the mise task that mints it. ``stores`` pairs each ``--store``
    flag with the projection variable whose field it fills, the secret first.
    ``bootstrap`` pairs each environment variable the task reads with the
    projection variable whose field on the bootstrap item holds it, so a
    bootstrap item is shaped like the connection.
    """

    task: str
    stores: tuple[tuple[str, str], ...]
    bootstrap: tuple[tuple[str, str], ...]
    # The flag naming the account, when the credential is minted for one.
    account_flag: str = ""

    @property
    def invocation(self) -> tuple[str, str, str]:
        """The words that run the task, from the checkout."""

        return ("mise", "run", self.task)


MINTERS: Mapping[str, Minter] = frozendict(
    {
        "cloudflare_api": Minter(
            task="mint:cloudflare",
            stores=(("--store", "API_TOKEN"),),
            bootstrap=(("CLOUDFLARE_BOOTSTRAP_TOKEN", "API_TOKEN"),),
            account_flag="--account",
        ),
        "tailscale": Minter(
            task="mint:tailscale",
            stores=(("--store", "CLIENT_SECRET"), ("--store-id", "CLIENT_ID")),
            bootstrap=(
                ("TAILSCALE_BOOTSTRAP_CLIENT_ID", "CLIENT_ID"),
                ("TAILSCALE_BOOTSTRAP_CLIENT_SECRET", "CLIENT_SECRET"),
            ),
        ),
    }
)
