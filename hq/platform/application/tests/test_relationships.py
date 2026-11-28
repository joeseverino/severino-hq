"""Entity pages read one relation graph, and every name links one way."""

import re

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core import testing
from hq.platform.core.testing import declare, operator

from ..entity_links import entity_link, kind_label
from ..tailnet import TAILNET_KIND

CONTROLLER = "example-controller"
ACCOUNT = "0123456789abcdef"


def store(kind, *records, reachable=True, connected=True, error=""):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "connected": connected,
            "error": error,
            "controller_id": CONTROLLER,
            "observed_at": timezone.now(),
        },
    )


def dns(name, content, *, proxied, record_type="A"):
    return {
        "zone": "example.com",
        "name": name,
        "record_type": record_type,
        "content": content,
        "proxied": proxied,
        "ttl": 1,
    }


def login(client, name="relations-op"):
    operator(client, name)


class LinkBuilderTests(TestCase):
    def test_an_unregistered_kind_is_refused(self):
        with self.assertRaises(KeyError):
            entity_link("example.unregistered", "x")

    def test_consoles_are_built_only_from_record_fields_the_schema_keeps(self):
        for kind, spec in OBSERVATIONS.items():
            with self.subTest(kind=kind):
                self.assertEqual(spec.console({}), "")


EDGE = {
    "connection_ref": "example-cloudflare",
    "account_id": ACCOUNT,
    "zone": "example.com",
    "id": "e1",
    "hosts": ["example.com", "*.example.com"],
    "status": "active",
    "certificate_authority": "google",
    "expires_on": "2099-01-01T00:00:00+00:00",
}


def estate(size):
    """``size`` machines on the tailnet, each serving one name in one domain."""

    testing.connection(PROVIDERS[TAILNET_KIND].connection_providers[0], "example-tailnet")
    testing.connection("cloudflare_api", "example-cloudflare")
    declare("example-com", "cloudflare.zone", {"zone": "example.com", "connection_ref": "example-cloudflare"})
    store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-cloudflare", "account_id": ACCOUNT})
    devices, records, apps = [], [], []
    for index in range(size):
        name, address = f"example-host-{index}", f"100.64.0.{index + 1}"
        declare(name, "machine", {"name": name, "addresses": [address]})
        declare(f"s{index}-dns", "adguard.rewrite", {"domain": f"s{index}.example.com", "answer": address})
        devices.append({"name": name, "addresses": [address], "dns_name": f"{name}.example.ts.net", "online": True})
        records.append(dns(f"s{index}.example.com", address, proxied=True))
        apps.append(
            {
                "connection_ref": "example-cloudflare",
                "account_id": ACCOUNT,
                "id": f"a{index}",
                "name": f"example-gate-{index}",
                "domain": f"s{index}.example.com",
            }
        )
    store(TAILNET_KIND, *devices)
    store("cloudflare.dns_record", *records)
    store("cloudflare.access_app", *apps)
    store("cloudflare.edge_certificate", EDGE)
    store(
        "cloudflare.pages_project",
        {
            "connection_ref": "example-cloudflare",
            "account_id": ACCOUNT,
            "name": "example-site",
            "domains": ["example.com"],
        },
    )


class NoRawKindsTests(TestCase):
    def test_the_machine_page_names_its_declarations_by_label(self):
        estate(1)
        declare("example-host-0-tailnet", TAILNET_KIND, {"name": "example-host-0", "key_expiry_disabled": False})
        login(self.client)

        page = self.client.get(reverse("control_plane:machine", args=["example-host-0"]))
        declaration = self.client.get(reverse("control_plane:detail", args=["example-host-0-tailnet"]))

        for response in (page, declaration):
            shown = re.sub(r"<pre\b[^>]*>.*?</pre>", "", response.content.decode(), flags=re.DOTALL)
            self.assertNotIn(TAILNET_KIND, shown)
            self.assertNotIn("cloudflare.dns_record", shown)
        self.assertContains(declaration, kind_label(TAILNET_KIND))
        # The declaration page leads back to the machine it belongs to.
        self.assertContains(declaration, f'href="{reverse("control_plane:machine", args=["example-host-0"])}"')


class DomainCardTests(TestCase):
    """Four cards; parked names, a weaker posture and missing mail policy said plainly."""

    def setUp(self):
        from .zone_records import APEX, record, sweep

        sweep(
            zones=[
                {
                    "zone": "example.com",
                    "connection_ref": "cf-example",
                    "posture": {"ssl": "strict", "min_tls_version": "1.3"},
                },
                {
                    "zone": "example.net",
                    "connection_ref": "cf-example",
                    "posture": {"ssl": "full", "min_tls_version": "1.0"},
                },
            ],
            records=[
                *APEX,
                record("example.net", "A", "192.0.2.1", zone="example.net", rid="n1", proxied=True),
                record("www.example.net", "CNAME", "example.net", zone="example.net", rid="n2", proxied=True),
            ],
        )
        for key, name, rtype, content in (
            ("net-apex", "example.net", "A", "192.0.2.1"),
            ("net-www", "www.example.net", "CNAME", "example.net"),
        ):
            declare(
                key,
                "cloudflare.dns_record",
                {
                    "zone": "example.net",
                    "name": name,
                    "record_type": rtype,
                    "content": content,
                    "proxied": True,
                    "ttl": 1,
                    "connection_ref": "cf-example",
                },
            )

    def cards(self, zone):
        from ..zones import find_zone

        return {card.label: card for card in find_zone(zone).insights}

    def test_a_parked_domain_says_so_with_who_handles_it(self):
        card = self.cards("example.net")["Services"]

        self.assertEqual(card.value, "Parked")
        self.assertIn("Cloudflare", card.detail)
