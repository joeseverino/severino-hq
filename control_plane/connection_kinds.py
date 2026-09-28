"""What each kind of connection is called, and how its credential can be held."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ConnectionKind:
    """One connection provider: its name on the page, and how its credential is held.

    ``credential`` is ``scoped`` or ``coarse``. The controller reports that a
    credential reached its endpoint, never what it is allowed to do, so the only
    honest statement about least privilege is the one the provider's credential
    model permits: a scoped provider issues narrow tokens whose grants HQ could
    verify; a coarse one issues a login or an admin token that is the whole
    account.
    """

    label: str
    credential: str


CONNECTION_KINDS: Mapping[str, ConnectionKind] = MappingProxyType(
    {
        "cloudflare_api": ConnectionKind("Cloudflare API", "scoped"),
        "cloudflare_dns": ConnectionKind("Cloudflare DNS", "scoped"),
        # A service account token is issued per vault and per permission, so the
        # one HQ carries can be write access to a single item's vault and
        # nothing else. That is the property the publishing adapter is built to
        # deserve rather than to rely on.
        "onepassword": ConnectionKind("1Password", "scoped"),
        "tailscale": ConnectionKind("Tailscale", "scoped"),
        # An app's permissions are fine-grained and each token HQ mints is
        # narrowed again to one call's repositories and permissions.
        "github_app": ConnectionKind("GitHub App", "scoped"),
        "adguard": ConnectionKind("AdGuard Home", "coarse"),
        "npm": ConnectionKind("Nginx Proxy Manager", "coarse"),
        "portainer": ConnectionKind("Portainer", "coarse"),
        "ssh": ConnectionKind("SSH", "coarse"),
    }
)

# The two facts most callers want, derived so neither can name a provider the
# other does not.
CONNECTION_CREDENTIALS: Mapping[str, str] = MappingProxyType(
    {provider: kind.credential for provider, kind in CONNECTION_KINDS.items()}
)
CONNECTION_LABELS: Mapping[str, str] = MappingProxyType(
    {provider: kind.label for provider, kind in CONNECTION_KINDS.items()}
)


def connection_credential(provider: str) -> str:
    """The credential model of one connection provider; blank when unnamed."""

    return CONNECTION_CREDENTIALS.get(provider, "")
