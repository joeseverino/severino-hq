"""What each kind of connection is called, and how its credential can be held."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# How each connection provider's credential can be held, declared once for
# every provider that names one. The controller reports that a credential reached
# its endpoint, never what it is allowed to do, so the only honest statement
# about least privilege is the one the provider's credential model permits: a
# scoped provider issues narrow tokens whose grants HQ could verify; a coarse
# one issues a login or an admin token that is the whole account.
CONNECTION_CREDENTIALS: Mapping[str, str] = MappingProxyType(
    {
        "cloudflare_api": "scoped",
        "cloudflare_dns": "scoped",
        # A service account token is issued per vault and per permission, so the
        # one HQ carries can be write access to a single item's vault and
        # nothing else. That is the property the publishing adapter is built to
        # deserve rather than to rely on.
        "onepassword": "scoped",
        "tailscale": "scoped",
        # An app's permissions are fine-grained and each token HQ mints is
        # narrowed again to one call's repositories and permissions.
        "github_app": "scoped",
        "adguard": "coarse",
        "npm": "coarse",
        "portainer": "coarse",
        "ssh": "coarse",
    }
)


def connection_credential(provider: str) -> str:
    """The credential model of one connection provider; blank when unnamed."""

    return CONNECTION_CREDENTIALS.get(provider, "")


# Each connection provider's name as the page shows it.
CONNECTION_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "cloudflare_api": "Cloudflare API",
        "cloudflare_dns": "Cloudflare DNS",
        "onepassword": "1Password",
        "tailscale": "Tailscale",
        "github_app": "GitHub App",
        "adguard": "AdGuard Home",
        "npm": "Nginx Proxy Manager",
        "portainer": "Portainer",
        "ssh": "SSH",
    }
)

if set(CONNECTION_LABELS) != set(CONNECTION_CREDENTIALS):
    raise ValueError("Every connection provider needs a label and a credential model.")
