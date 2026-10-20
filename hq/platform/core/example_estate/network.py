"""How names resolve and who can reach what: the tailnet, AdGuard on the LAN,
and the public zone at Cloudflare."""

import json
from datetime import timedelta

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.inventory_testing import store

from .names import (
    ADGUARD,
    CLOUDFLARE,
    DNS,
    HQ,
    LAN,
    LAPTOP,
    LAPTOP_NAME,
    MACHINE,
    OWNER,
    PHONE,
    PHONE_NAME,
    PUBLIC,
    SERVICES,
    TAILNET,
    TAILNET_V4,
    TAILNET_V6,
    ZONE,
    hexed,
    iso,
    magic_dns,
    now,
    tailnet_v4,
    tailnet_v6,
)

# The tailnet: the owner reaches the server on 443, and nothing else is granted.
POLICY = {
    "groups": {"group:admins": [OWNER]},
    "tagOwners": {"tag:server": ["group:admins"]},
    "grants": [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:443"]}],
    "tests": [{"src": OWNER, "accept": ["tag:server:443"]}],
}
SETTINGS = {
    "devicesApprovalOn": True,
    "usersApprovalOn": True,
    "devicesAutoUpdatesOn": True,
    "devicesKeyDurationDays": 180,
    "httpsEnabled": True,
    "aclsExternallyManagedOn": True,
}
# A domain that sends and takes no mail, and says so: a null MX, an SPF that
# allows nobody, a DKIM key revoked for every selector and a DMARC policy that
# rejects the rest. Beside that, what a zone gathers over a few years: a
# website behind the proxy, a status page and docs hosted elsewhere, and the
# verifications services asked for. Only an A, AAAA or CNAME record names a
# service, so each of those has something behind it (a proxied site, a host
# elsewhere); the apex is dual stack, and two records of one type on a name are
# a contradiction HQ reports. The rest are policy.
# Key, name, type, content, priority, proxied.
PUBLIC_RECORDS = (
    ("apex", ZONE, "A", "203.0.113.24", None, True),
    ("apex-v6", ZONE, "AAAA", "2001:db8::24", None, True),
    ("www", f"www.{ZONE}", "CNAME", ZONE, None, True),
    ("status", f"status.{ZONE}", "CNAME", "example-status.statuspage.example.net", None, False),
    ("docs", f"docs.{ZONE}", "CNAME", "example-docs.pages.example.net", None, False),
    ("mx", ZONE, "MX", ".", 0, False),
    ("spf", ZONE, "TXT", "v=spf1 -all", None, False),
    ("dkim", f"*._domainkey.{ZONE}", "TXT", "v=DKIM1; p=", None, False),
    (
        "dmarc",
        f"_dmarc.{ZONE}",
        "TXT",
        "v=DMARC1; p=reject; sp=reject; adkim=s; aspf=s; rua=mailto:dmarc-reports@example.com",
        None,
        False,
    ),
    ("site-verification", ZONE, "TXT", "example-site-verification=Xq3vLm9aT0pR7wKc2YhN5bDf8GjE1sUo4iZ6", None, False),
    ("atproto", f"_atproto.{ZONE}", "TXT", "did=did:plc:example7q3vlm9at0pr7wkc2yhn5bdf", None, False),
    ("domain-verification", f"_example-verification.{ZONE}", "TXT", "example-verify=b4d1e8c27f09a3561d", None, False),
    ("pages-challenge", f"_example-pages-challenge.{ZONE}", "TXT", "6f1c0b9e2d4a8b7c3e5f0a1d9c8b7a", None, False),
    ("caa", ZONE, "CAA", '0 issue "letsencrypt.org"', None, False),
    ("caa-wild", ZONE, "CAA", '0 issuewild "letsencrypt.org"', None, False),
    ("caa-pki", ZONE, "CAA", '0 issue "ca.example.net; validationmethods=dns-01"', None, False),
    ("caa-iodef", ZONE, "CAA", '0 iodef "mailto:security@example.com"', None, False),
    (
        "security",
        f"_security.{ZONE}",
        "TXT",
        "security_policy=https://example.com/.well-known/security.txt",
        None,
        False,
    ),
)
RENEWS_IN = timedelta(days=200)


def declare_rewrites():
    """Each name resolves on the LAN and the tailnet through AdGuard."""

    for hostname, container, _port, _image in SERVICES:
        ManagedResource.objects.create(key=f"{container}-dns", kind="adguard.rewrite", spec=_rewrite(hostname))


def _rewrite(hostname):
    return {"domain": hostname, "answer": TAILNET if hostname == HQ else LAN, "connection_ref": ADGUARD}


def declare_zone():
    ManagedResource.objects.create(
        key="example-zone", kind="cloudflare.zone", spec={"zone": ZONE, "connection_ref": DNS}
    )
    for key, *fields in PUBLIC_RECORDS:
        ManagedResource.objects.create(key=f"example-{key}", kind="cloudflare.dns_record", spec=_record(*fields))


def _record(name, kind, content, priority, proxied):
    return {
        "zone": ZONE,
        "name": name,
        "record_type": kind,
        "content": content,
        # Cloudflare sets a proxied record's TTL itself, and says 1 for it.
        "ttl": 1 if proxied else 3600,
        "proxied": proxied,
        "priority": priority,
        "connection_ref": DNS,
    }


def zone():
    """What Cloudflare and the public registry report about the domain."""

    expires = (now() + RENEWS_IN).isoformat()
    store("cloudflare.dns_record", *(_record(*fields) for _key, *fields in PUBLIC_RECORDS))
    store(
        "cloudflare.zone",
        {
            "zone": ZONE,
            "connection_ref": DNS,
            "posture": {"ssl": "strict", "min_tls_version": "1.2", "always_use_https": "on"},
            "registration": {"registrar": "Example Registrar", "expires_at": expires, "auto_renew": True},
        },
    )
    store(
        "registry.domain",
        {
            "domain": ZONE,
            "registrar": "Example Registrar",
            "registered_at": "2019-04-02T00:00:00Z",
            "expires_at": expires,
            "status": ["client transfer prohibited"],
            "read_at": iso(timedelta(hours=3)),
        },
    )
    store(
        "cloudflare.edge_certificate",
        {
            "connection_ref": CLOUDFLARE,
            "zone": ZONE,
            "id": "example-edge",
            "type": "universal",
            "hosts": [ZONE, f"*.{ZONE}"],
            "status": "active",
            "certificate_authority": "google",
            "expires_on": (now() + timedelta(days=71)).isoformat(),
        },
    )
    # The rest of the account: nothing on Pages, D1, Access or tunnels.
    for kind in (
        "cloudflare.pages_project",
        "cloudflare.d1_database",
        "cloudflare.access_app",
        "cloudflare.access_service_token",
        "cloudflare.tunnel",
        "cloudflare.redirect",
    ):
        store(kind)


def adguard():
    """The rewrites as AdGuard holds them, its settings, clients and lookups."""

    store("adguard.rewrite", *(_rewrite(hostname) for hostname, *_rest in SERVICES))
    store(
        "adguard.dns",
        {
            "connection_ref": ADGUARD,
            "version": "v0.107.55",
            "running": True,
            "protection_enabled": True,
            "dns_addresses": [LAN, TAILNET],
            "upstreams": [{"host": "dns.quad9.net", "transport": "https", "domains": []}],
            "upstream_mode": "load_balance",
            "dnssec_enabled": True,
            "filtering_enabled": True,
            "filter_lists": 3,
            "filter_rules": 182_406,
            "rewrites_enabled": True,
            "querylog_enabled": True,
            "querylog_retention_hours": 2160.0,
            "anonymize_client_ip": False,
        },
    )
    clients = ((LAPTOP_NAME, LAPTOP, 3), (PHONE_NAME, PHONE, 1))
    store(
        "adguard.client",
        *(
            {
                "connection_ref": ADGUARD,
                "name": name,
                "source": "persistent",
                "ids": [address],
                "addresses": [address],
                "use_global_settings": True,
                "filtering_enabled": True,
            }
            for name, address, _weight in clients
        ),
    )
    store(
        "adguard.query_summary",
        *(
            {
                "connection_ref": ADGUARD,
                "domain": hostname,
                "queries": 40 * port % 700 + 120,
                "blocked": 0,
                "client_count": len(clients),
                "clients": [
                    {"address": address, "name": name, "queries": weight * (40 * port % 700 + 120) // 4}
                    for name, address, weight in clients
                ],
                "last_seen": iso(timedelta(minutes=2)),
                "window_hours": 24.0,
            }
            for hostname, _container, port, _image in SERVICES
        ),
    )


def tailnet():
    """The host, the laptop the demo is read from, and a phone; the policy and lock."""

    at = now()

    # As the controller writes a device: both addresses, the MagicDNS name
    # without its trailing dot, the node key and the client's full version.
    def device(name, os, **extra):
        return {
            "name": name,
            "addresses": [tailnet_v4(name), tailnet_v6(name)],
            "dns_name": magic_dns(name),
            "os": os,
            "online": True,
            "public_key": hexed("node", name),
            "client_version": "1.76.6-t3b1a6e2c4-g5e7f9a1b3",
            "last_handshake": (at - timedelta(minutes=1)).isoformat(),
            **extra,
        }

    reach = [{"port": 443, "who": [OWNER], "rules": [{"who": [OWNER], "to": ["tag:server:443"], "line": 1}]}]
    store(
        "tailscale.device",
        device(
            MACHINE,
            "linux",
            self=True,
            tags=["tag:server"],
            reach=reach,
            endpoints=[f"{LAN}:41641", f"{PUBLIC}:41641", "[2001:db8:4f1a:2e00:5054:ff:fe12:3456]:41641"],
        ),
        device(
            LAPTOP_NAME,
            "macOS",
            user=OWNER,
            active=True,
            direct_endpoint=f"{PUBLIC}:41641",
            rx_bytes=48_213_904,
            tx_bytes=6_120_448,
        ),
        device(PHONE_NAME, "iOS", user=OWNER),
    )
    store(
        "tailscale.policy",
        {
            "record": "policy",
            "document": json.dumps(POLICY, indent=2),
            "groups": [{"name": "group:admins", "members": [OWNER]}],
            "tags": [{"name": "tag:server", "owners": ["group:admins"]}],
            "grants": POLICY["grants"],
            "tests": POLICY["tests"],
            "settings": SETTINGS,
            "dns": {"dns": [TAILNET], "magicDNS": True},
            "lock": {"enabled": True, "trusted_keys": 2},
        },
    )
    store(
        "tailscale.settings",
        {
            "record": "settings",
            "devices_approval_on": True,
            "devices_key_duration_days": 180,
            "devices_auto_updates_on": True,
            "users_approval_on": True,
            "regional_routing_on": False,
            "posture_identity_collection_on": False,
            "https_enabled": True,
            "acls_externally_managed_on": True,
        },
    )
    store(
        "tailscale.dns",
        {"record": "dns", "nameservers": [TAILNET], "override_local_dns": True, "magic_dns": True, "search_paths": []},
    )
    store(
        "tailscale.user",
        {
            "id": "example-user",
            "display_name": "Example",
            "login_name": OWNER,
            "role": "owner",
            "status": "active",
            "created": "2024-01-08T10:00:00Z",
            "last_seen": iso(timedelta(minutes=1)),
        },
    )


# Only the tailnet reaches the proxy.
TAILNET_ONLY = {
    "satisfy_any": False,
    "pass_auth": False,
    "authorization_count": 0,
    "implicit_deny": True,
    "clients": [
        {"directive": "allow", "address": str(TAILNET_V4)},
        {"directive": "allow", "address": str(TAILNET_V6)},
    ],
}
