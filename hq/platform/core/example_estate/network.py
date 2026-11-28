"""How names resolve and who can reach what: the tailnet, AdGuard on the LAN,
and the public zone at Cloudflare."""

import json
from datetime import timedelta

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.providers import record_spec

from .names import (
    ADGUARD,
    CLOUDFLARE,
    DNS,
    HQ,
    LAN,
    LAN_SUBNET,
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
    node_id,
    now,
    tailnet_v4,
    tailnet_v6,
)
from .sweep import store

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
# The laptop's node key, signed in to months ago, runs out inside the window
# HQ warns in: the one tailnet finding the estate raises.
LAPTOP_KEY_LEFT = timedelta(days=30)
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
# A rewrite made in AdGuard's own screen for the printer, which no declaration
# claims: the one name HQ offers to take under its management.
HAND_MADE = {"domain": "printer.example.com", "answer": "10.100.200.47", "connection_ref": ADGUARD}
RENEWS_IN = timedelta(days=200)
# The zone's declaration, keyed by its name the way a key is written.
ZONE_KEY = ZONE.replace(".", "-")


def declare_rewrites():
    """Each name resolves on the LAN and the tailnet through AdGuard."""

    for hostname, container, _port, _image in SERVICES:
        # Declared as the rewrite AdGuard holds, which names the connection it was read through.
        spec = record_spec("adguard.rewrite", _rewrite(hostname))
        ManagedResource.objects.create(key=f"{container}-dns", kind="adguard.rewrite", spec=spec)


def _rewrite(hostname):
    return {"domain": hostname, "answer": TAILNET if hostname == HQ else LAN, "connection_ref": ADGUARD}


def declare_zone():
    ManagedResource.objects.create(key=ZONE_KEY, kind="cloudflare.zone", spec={"zone": ZONE, "connection_ref": DNS})
    for key, *fields in PUBLIC_RECORDS:
        spec = record_spec("cloudflare.dns_record", _record(*fields))
        ManagedResource.objects.create(key=f"{ZONE_KEY}-{key}", kind="cloudflare.dns_record", spec=spec)


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
            "registration": {"registrar": "Cloudflare, Inc.", "expires_at": expires, "auto_renew": True},
        },
    )
    store(
        "registry.domain",
        {
            "domain": ZONE,
            "registrar": "Cloudflare, Inc.",
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
    # The events Worker, deployed before HQ's routes last changed.
    store(
        "cloudflare.worker",
        {
            "connection_ref": CLOUDFLARE,
            "account_id": "0123abcd",
            "name": "hq-events",
            "deployment_id": "example-deployment",
            "deployed_at": iso(timedelta(days=6)),
            "versions": [{"id": "example-version", "number": 14, "percentage": 100, "tag": ""}],
            "requests": 1_284,
            "errors": 3,
            "cpu_time_p50_us": 812.0,
            "cpu_time_p99_us": 4_210.0,
            # Tailscale signs with a secret the Worker no longer holds, so
            # every one of its deliveries is refused.
            "answers": [
                {"route": "/github", "status": 202, "requests": 1_240, "last_hour": 41},
                {"route": "/github", "status": 401, "requests": 3, "last_hour": 0},
                {"route": "/github", "status": 413, "requests": 1, "last_hour": 0},
                {"route": "/tailscale", "status": 401, "requests": 34, "last_hour": 12},
                {"route": "/cloudflare", "status": 202, "requests": 2, "last_hour": 1},
                {"route": "/socket", "status": 101, "requests": 6, "last_hour": 0},
            ],
        },
    )
    # The edge certificate's renewal, as the alert history keeps it.
    store(
        "cloudflare.alert",
        {
            "connection_ref": CLOUDFLARE,
            "account_id": "0123abcd",
            "id": "00000000-0000-4000-8000-0000000000a1",
            "alert_type": "universal_ssl_event_type",
            "name": "Certificate alerts",
            "policy_id": "00000000-0000-4000-8000-0000000000a2",
            "description": "Universal certificate issuance and renewal.",
            "mechanism_type": "webhook",
            "sent": iso(timedelta(minutes=9)),
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


# What a zone left to itself drifts into, one of each thing HQ finds wrong
# about a domain (``zone_findings``): challenges an issuance left behind, a CAA
# record that refuses the authority renewing its certificate, an SPF past its
# lookup limit, a DMARC policy that delivers forgeries, a weaker TLS posture
# than its neighbor's, and a registration that will not renew.
NEGLECTED = (
    ("challenge", f"_acme-challenge.{ZONE}", "TXT", "example-challenge-token-one", None, False),
    ("challenge-www", f"_acme-challenge.www.{ZONE}", "TXT", "example-challenge-token-two", None, False),
    ("caa", ZONE, "CAA", '0 issue "ca.example.net"', None, False),
    (
        "spf",
        ZONE,
        "TXT",
        "v=spf1 " + " ".join(f"include:_spf{number}.example.net" for number in range(11)) + " -all",
        None,
        False,
    ),
    ("dmarc", f"_dmarc.{ZONE}", "TXT", "v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com", None, False),
)
NEIGHBOR = "example.net"


def neglected():
    """The domain as ``NEGLECTED`` leaves it, beside a neighbor held to a stronger posture."""

    # Each record of the zone, one of the same key as it was left instead,
    # declared as it was left, so HQ's record and the zone agree.
    dropped = {"caa-wild", "caa-pki"}
    ManagedResource.objects.filter(key__in=[f"{ZONE_KEY}-{key}" for key in dropped]).delete()
    for key, *fields in NEGLECTED:
        spec = record_spec("cloudflare.dns_record", _record(*fields))
        ManagedResource.objects.filter(key=f"{ZONE_KEY}-{key}").update(spec=spec)
    kept = {key: fields for key, *fields in (*PUBLIC_RECORDS, *NEGLECTED) if key not in dropped}
    store("cloudflare.dns_record", *(_record(*fields) for fields in kept.values()))
    store(
        "cloudflare.zone",
        {
            "zone": ZONE,
            "connection_ref": DNS,
            "posture": {"ssl": "full", "min_tls_version": "1.0"},
            "registration": {
                "registrar": "Cloudflare, Inc.",
                "expires_at": (now() + timedelta(days=40)).isoformat(),
                "auto_renew": False,
            },
        },
        {"zone": NEIGHBOR, "connection_ref": DNS, "posture": {"ssl": "strict", "min_tls_version": "1.2"}},
    )


def adguard():
    """The rewrites as AdGuard holds them, its settings, clients and lookups."""

    store("adguard.rewrite", *(_rewrite(hostname) for hostname, *_rest in SERVICES), HAND_MADE)
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


# What the machine's owner named its tailnet device, and what the LAN calls
# it by: one spelling, a machine's name and a DNS name at once.
SHORT_NAME = "homebox"


def short_named():
    """The machine's tailnet device renamed to its short LAN name, which an
    AdGuard rewrite also answers and the LAN looks up: the machine's page
    shows the device and never that name's lookups or rewrite."""

    devices = ProviderInventory.objects.get(kind="tailscale.device")
    for device in devices.records:
        if device["name"] == MACHINE:
            device.update(name=SHORT_NAME, dns_name=magic_dns(SHORT_NAME))
    devices.save(update_fields=["records"])
    for kind, record in (
        ("adguard.rewrite", {"domain": SHORT_NAME, "answer": LAN, "connection_ref": ADGUARD}),
        (
            "adguard.query_summary",
            {"connection_ref": ADGUARD, "domain": SHORT_NAME, "queries": 42, "last_seen": iso(timedelta(minutes=2))},
        ),
    ):
        held = ProviderInventory.objects.get(kind=kind)
        held.records = [*held.records, record]
        held.save(update_fields=["records"])


def lock_key(name):
    """A device's tailnet lock public key, as the controller writes one."""

    return f"tlpub:{hexed('lock', name)}"


def tailnet():
    """The host, the laptop the demo is read from, and a phone that is away;
    the policy and lock, which the phone and the laptop sign for."""

    at = now()

    # As the controller writes a device: its stable id, both addresses, the
    # MagicDNS name without its trailing dot, the node key and the client's
    # full version.
    def device(name, os, **extra):
        return {
            "name": name,
            "node_id": node_id(name),
            "addresses": [tailnet_v4(name), tailnet_v6(name)],
            "dns_name": magic_dns(name),
            "os": os,
            "online": True,
            "public_key": hexed("node", name),
            "lock_key": lock_key(name),
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
            # Its LAN offered as a subnet route, and approved.
            advertised_routes=[LAN_SUBNET],
            enabled_routes=[LAN_SUBNET],
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
            key_expires=(at + LAPTOP_KEY_LEFT).isoformat(),
        ),
        # Off the tailnet since yesterday, so the machines list says it is away.
        device(PHONE_NAME, "iOS", user=OWNER, online=False, last_seen=(at - timedelta(days=1)).isoformat()),
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
        },
    )
    store(
        "host.tailnet_lock",
        {
            "record": "lock",
            "enabled": True,
            "node_key_signed": True,
            "signers": [{"key": lock_key(name), "kind": "25519", "votes": 1} for name in (PHONE_NAME, LAPTOP_NAME)],
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
