"""A zone apex that holds nine records for one name, and the sweep that finds them."""

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

# Every DNS connection these tests name, each managing.
DNS_CONNECTIONS = tuple(("cloudflare_dns", ref) for ref in ("cf-example", "cf", "a-dns", "a-dns-account"))

ZONE_KIND = "cloudflare.zone"
RECORD_KIND = "cloudflare.dns_record"


def record(name, rtype, content, *, zone="example.com", rid="x", **extra):
    return {
        "zone": zone,
        "record_id": rid,
        "name": name,
        "record_type": rtype,
        "content": content,
        "priority": extra.get("priority"),
        "proxied": extra.get("proxied", False),
        "ttl": extra.get("ttl", 1),
    }


# One name, nine records. This is the fixture the whole module exists for.
APEX = [
    record("example.com", "CNAME", "example.pages.dev", rid="r1", proxied=True),
    record("example.com", "MX", "mx01.mail.example.net", rid="r2", priority=10),
    record("example.com", "MX", "mx02.mail.example.net", rid="r3", priority=20),
    record("example.com", "TXT", '"v=spf1 include:example.net -all"', rid="r4"),
    record("example.com", "TXT", '"apple-domain=A1B2C3D4"', rid="r5"),
    record("example.com", "TXT", '"openai-domain-verification=dv-x"', rid="r6"),
    record("example.com", "CAA", '0 issue "letsencrypt.org"', rid="r7"),
    record("example.com", "CAA", '0 issuewild "letsencrypt.org"', rid="r8"),
    record("example.com", "CAA", '0 iodef "mailto:security@example.com"', rid="r9"),
]
ZONES = [{"zone": "example.com", "connection_ref": "cf-example"}]


def sweep(zones=ZONES, records=APEX):
    now = timezone.now()
    for kind, rows in ((ZONE_KIND, zones), (RECORD_KIND, records)):
        ProviderInventory.objects.update_or_create(
            kind=kind,
            defaults={
                "records": rows,
                "reachable": True,
                "error": "",
                "observed_at": now,
                "controller_id": "test",
            },
        )
