"""The showcase's names, addresses and the stable values derived from them."""

import hashlib
from datetime import UTC, timedelta

from django.utils import timezone

from hq.platform.application import reach

MACHINE = "app-host"
LAN = "10.0.0.10"
# Addresses in the range only Tailscale issues, as HQ declares it.
TAILNET_V4, TAILNET_V6 = reach.TAILNET
TAILNET = str(TAILNET_V4[10])
HQ = "hq.example.com"
ZONE = "example.com"
PROXY = "172.18.0.2"
LAPTOP = str(TAILNET_V4[20])
PHONE = str(TAILNET_V4[21])
# The home's public address, from a documentation range.
PUBLIC = "198.51.100.7"
OWNER = "example@example.com"
SSO = "hq.platform.core.oidc.HQOIDCAuthenticationBackend"

PORTAINER = "example-portainer"
DNS = "example-dns"
CLOUDFLARE = "example-cloudflare"
PROXY_REF = "example-proxy"
ADGUARD = "example-adguard"
TAILSCALE = "example-tailnet"
SSH = "example-ssh"
GITHUB = "github"


def hexed(*parts, length=64):
    """A stable hex string for ``parts``: the same build gives the same values."""

    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:length]


def container_id(name):
    """A stable, Docker-shaped short ID for a container."""

    return hexed(name)[:12]


def digest(*parts):
    return f"sha256:{hexed('digest', *parts)}"


# The commit HQ was built from and the digest of the image that carries it.
HQ_REVISION = hexed("hq", "revision", length=40)
HQ_DIGEST = digest("ghcr.io/example/hq", "2.4.0")
HQ_SOURCE = "https://github.com/example/hq"

# Hostname, container, port, image: what the proxy routes and where it lands.
SERVICES = (
    (HQ, "hq", 8000, f"ghcr.io/example/hq:2.4.0@{HQ_DIGEST}"),
    ("photos.example.com", "immich", 2283, "ghcr.io/immich-app/immich-server:v1.120.0"),
    ("media.example.com", "jellyfin", 8096, "jellyfin/jellyfin:10.10.0"),
    ("git.example.com", "forgejo", 3000, "codeberg.org/forgejo/forgejo:9.0.1"),
)
# Containers that run the estate rather than serve a name.
SUPPORT = (
    ("adguard", 3080, "adguard/adguardhome:v0.107.55"),
    ("nginx-proxy-manager", 81, "jc21/nginx-proxy-manager:2.12.1"),
    ("portainer", 9443, "portainer/portainer-ce:2.24.0"),
)
# Every container: name, port, image.
CONTAINERS = (*((c, p, i) for _h, c, p, i in SERVICES), *SUPPORT)
# Where each image's source lives, as its build labels say.
SOURCES = {
    "hq": HQ_SOURCE,
    "immich": "https://github.com/immich-app/immich",
    "jellyfin": "https://github.com/jellyfin/jellyfin",
    "forgejo": "https://github.com/forgejo/forgejo",
    "adguard": "https://github.com/AdguardTeam/AdGuardHome",
    "nginx-proxy-manager": "https://github.com/NginxProxyManager/nginx-proxy-manager",
    "portainer": "https://github.com/portainer/portainer",
}


def now():
    return timezone.now()


def stamp(age=timedelta(0)):
    """An instant ``age`` ago in UTC, as systemd and the registries write one."""

    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(age=timedelta(0)):
    return (timezone.now() - age).isoformat()
