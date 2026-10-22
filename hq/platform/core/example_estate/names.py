"""The showcase's names, addresses and the stable values derived from them."""

import hashlib
from datetime import UTC, timedelta

from django.utils import timezone

from hq.platform.application import reach

MACHINE = "example-docker-host-01"
# Made up, at the length a home LAN's addresses run to.
LAN = "10.100.200.214"
LAN_SUBNET = "10.100.200.0/24"
HQ = "hq.example.com"
ZONE = "example.com"
PROXY = "172.18.0.2"
# The tailnet's devices besides the machine, as their owner named them.
LAPTOP_NAME = "example-macbook-pro-16"
PHONE_NAME = "example-iphone-16-pro"
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
# A shell connection is named for the machine it opens, which is what makes it
# that machine rather than a fifth one.
SSH = MACHINE
GITHUB = "github"


def hexed(*parts, length=64):
    """A stable hex string for ``parts``: the same build gives the same values."""

    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:length]


# The ranges only Tailscale issues, as HQ declares them, and a MagicDNS domain
# in the form Tailscale assigns one, its label made up.
TAILNET_V4, TAILNET_V6 = reach.TAILNET
MAGIC_DNS = "tailc0ffee.ts.net"
# Each device's place in the tailnet. Its addresses are patterned, so plainly
# made up, and as long as real ones run.
DEVICES = {MACHINE: 103, LAPTOP_NAME: 104, PHONE_NAME: 105}


def tailnet_v4(device):
    return f"100.101.102.{DEVICES[device]}"


def tailnet_v6(device):
    """Its IPv6 address, which every device has beside its IPv4 one."""

    return str(TAILNET_V6[0x1001_2002_3003_4004_5000 + DEVICES[device]])


def magic_dns(device):
    return f"{device}.{MAGIC_DNS}"


TAILNET = tailnet_v4(MACHINE)
TAILNET6 = tailnet_v6(MACHINE)
LAPTOP = tailnet_v4(LAPTOP_NAME)
PHONE = tailnet_v4(PHONE_NAME)


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
    ("streaming.example.com", "jellyfin", 8096, "jellyfin/jellyfin:10.10.0"),
    ("git.example.com", "forgejo", 3000, "codeberg.org/forgejo/forgejo:9.0.1"),
    # A long name, and the long tags some publishers build: version, base and build.
    (
        "home-automation-dashboard.example.com",
        "home-automation",
        8123,
        "registry.example.net/example-builds/home-automation-server:2024.11.3-ubu2404-ls45",
    ),
)
# Containers that run the estate rather than serve a name.
SUPPORT = (
    ("adguard", 3080, "adguard/adguardhome:v0.107.55"),
    ("nginx-proxy-manager", 81, "jc21/nginx-proxy-manager:2.12.1"),
    ("portainer", 9443, "portainer/portainer-ce:2.24.0"),
    # Named the way Compose names a container: project, service, replica.
    ("portainer-agent-portainer-agent-1", 9001, "portainer/agent:2.24.0"),
)
# Every container: name, port, image.
CONTAINERS = (*((c, p, i) for _h, c, p, i in SERVICES), *SUPPORT)
# Where each image's source lives, as its build labels say.
SOURCES = {
    "hq": HQ_SOURCE,
    "immich": "https://github.com/immich-app/immich",
    "jellyfin": "https://github.com/jellyfin/jellyfin",
    "home-automation": "https://github.com/example-builds/home-automation-server",
    "forgejo": "https://github.com/forgejo/forgejo",
    "adguard": "https://github.com/AdguardTeam/AdGuardHome",
    "nginx-proxy-manager": "https://github.com/NginxProxyManager/nginx-proxy-manager",
    "portainer": "https://github.com/portainer/portainer",
    "portainer-agent-portainer-agent-1": "https://github.com/portainer/agent",
}


# A release published since the version that runs: the one upgrade HQ has to offer.
NEWER = {"home-automation": "2024.12.1-ubu2404-ls48"}


def now():
    return timezone.now()


def stamp(age=timedelta(0)):
    """An instant ``age`` ago in UTC, as systemd and the registries write one."""

    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(age=timedelta(0)):
    return (timezone.now() - age).isoformat()
