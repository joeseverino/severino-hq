"""The read permissions an observer credential needs, per connection provider.

A registered reading states its own ``requires``; the reads below run through
a credential but are not registered readings yet, and each entry leaves this
list when its read becomes one. Each name is a member of its provider's table
in ``permissions``.
"""

from collections.abc import Mapping

from .observations import OBSERVATIONS
from .permissions import Cloudflare, Permission, Tailscale
lazy from .connection_kinds import CONNECTION_KINDS
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


def credential_permissions(provider: str) -> tuple[Permission, ...]:
    """Every permission a scoped credential for ``provider`` is issued with,
    sorted: what its observer reads need and the writes its kind declares.
    Setup asks for these, and a replacement is minted with them."""

    kind = CONNECTION_KINDS.get(provider)
    return tuple(sorted({*observer_permissions(provider), *(kind.writes if kind else ())}))
