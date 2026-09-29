"""The sweep reads the AdGuard readings through the controller runtime, per connection."""

from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase

from application.inventory import record_inventory
from application.security import cli_principal
from control_plane.models import ProviderInventory
from control_plane.observations.adguard import CLIENT_KIND, DNS_KIND, QUERY_KIND

from .. import providers
from controller_runtime import provider_http

ENV = {
    "ADGUARD_URL": "https://adguard.example.test",
    "ADGUARD_USERNAME": "example",
    "ADGUARD_PASSWORD": "never-stored-secret",
    "ADGUARD_CONNECTION_REF": "example-adguard",
}

ANSWERS = {
    "/control/rewrite/list": [{"domain": "app.example.com", "answer": "100.64.0.10"}],
    "/control/clients": {"clients": [], "auto_clients": [
        {"ip": "100.64.0.2", "name": "laptop", "source": "ARP"}]},
    "/control/querylog/config": {"enabled": True, "interval": 86_400_000,
                                 "anonymize_client_ip": False},
    "/control/querylog": {"data": [], "oldest": ""},
    "/control/status": {"version": "v0.107.0", "protection_enabled": True,
                        "dns_addresses": ["100.64.0.53"]},
    "/control/dns_info": {"upstream_dns": ["tls://dns.example.test"]},
    "/control/filtering/status": {"enabled": True, "filters": []},
    "/control/rewrite/settings": {"enabled": True},
}


def answer(url, **kwargs):
    path = url.removeprefix(ENV["ADGUARD_URL"]).split("?", 1)[0]
    return ANSWERS[path]


@mock.patch.dict("os.environ", ENV, clear=True)
class AdGuardSweepTests(TestCase):
    def test_every_reading_is_swept_attributed_and_stored_without_the_secret(self):
        kinds = (CLIENT_KIND, QUERY_KIND, DNS_KIND, "adguard.rewrite")
        readers = {kind: providers.PROVIDER_INVENTORY[kind] for kind in kinds}
        with (
            mock.patch.object(provider_http, "request_json", side_effect=answer),
            mock.patch.dict(providers.PROVIDER_INVENTORY, readers, clear=True),
            provider_http.provider_snapshot(),
        ):
            report = providers.inventory()
        record_inventory(report, principal=cli_principal())

        for kind in kinds:
            with self.subTest(kind):
                stored = ProviderInventory.objects.get(kind=kind)
                self.assertTrue(stored.reachable and stored.connected)
                self.assertTrue(stored.records)
                self.assertEqual(
                    {record["connection_ref"] for record in stored.records}, {"example-adguard"}
                )
                self.assertNotIn(ENV["ADGUARD_PASSWORD"], json.dumps(stored.records))
        (summary,) = ProviderInventory.objects.get(kind=QUERY_KIND).records
        self.assertEqual((summary["domain"], summary["queries"]), ("app.example.com", 0))
