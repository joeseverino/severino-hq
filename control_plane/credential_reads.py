"""The read permissions an observer credential needs, per connection provider.

A registered reading states its own ``requires``. The reads below run through
a credential but are not registered readings yet; each entry leaves this list
when its read becomes one.

Cloudflare entries are ``<permission group name> (<account|zone>)``. Tailscale
entries are bare scope names.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .observations import OBSERVATIONS

# What the zone sweep's registrar read needs: expiry and auto-renew.
REGISTRAR_READ = "Registrar: Domains Read (account)"

UNREGISTERED_READS: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        "cloudflare_api": (
            ("Account Settings Read (account)", "The account and its analytics sites"),
            ("Account Analytics Read (account)", "Site analytics"),
            ("Zone Settings Read (zone)", "Zone TLS posture"),
            (REGISTRAR_READ, "Domain registration and auto-renew"),
        ),
        "tailscale": (
            ("devices:core:read", "Tailnet devices"),
            ("devices:routes:read", "Advertised and approved routes"),
            ("dns:read", "Tailnet DNS"),
            ("feature_settings:read", "Tailnet settings"),
            ("services:read", "Tailnet services"),
            ("policy_file:read", "Tailnet policy"),
        ),
    }
)


def observer_permissions(provider: str) -> tuple[str, ...]:
    """Every read permission this provider's observer credential needs, sorted."""

    found = {
        name
        for spec in OBSERVATIONS.values()
        if spec.provider == provider
        for name in spec.requires
    }
    found.update(name for name, _ in UNREGISTERED_READS.get(provider, ()))
    return tuple(sorted(found))
