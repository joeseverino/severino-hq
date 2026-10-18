"""A credential value planted where a controller could carry one, and the check that it never comes back out."""

import json
from typing import Any

from hq.domains.control_plane.models import ManagedResource

from ..report_testing import report_connections
from ..security import cli_principal
from ..sweep import record_sweep

SENTINEL = "SENTINEL-c0ffee-not-a-secret"
CONNECTION = "sentinel-dns"
REWRITE = "sentinel.example.test"
RESOURCE = "sentinel-rewrite"
READING = "host.perimeter"


def plant_credentials() -> None:
    """Report a connection and a sweep that each carry ``SENTINEL`` beside what HQ keeps.

    The connection's store holds the value next to its references, as a
    controller that read the item whole would send it; the sweep's record of a
    declared rewrite and of a reading carry it in fields a provider answer
    could include.
    """

    ManagedResource.objects.create(
        key=RESOURCE, kind="adguard.rewrite", spec={"domain": REWRITE, "answer": "192.0.2.10"}
    )
    report_connections(
        [
            {
                "connection_ref": CONNECTION,
                "provider": "cloudflare_dns",
                "reaches": ["example.com"],
                "store": {
                    "vault": "Example Vault",
                    "item": "exampleitem01",
                    "credential": SENTINEL,
                    "API_TOKEN": SENTINEL,
                },
            }
        ],
        controller_id="controller",
    )
    record_sweep(
        {
            "adguard.rewrite": {
                "ok": True,
                "records": [
                    {"domain": REWRITE, "answer": "192.0.2.10", "password": SENTINEL, "token": SENTINEL},
                ],
            },
            READING: {
                "ok": True,
                "records": [
                    {
                        "record": "perimeter",
                        "connection_ref": CONNECTION,
                        "answered_publicly": [443],
                        "token": SENTINEL,
                    },
                ],
            },
        },
        principal=cli_principal(),
        controller_id="controller",
    )


class SentinelMixin:
    def assertNoSentinel(self, payload: Any) -> None:
        text = payload if isinstance(payload, str | bytes) else json.dumps(payload, default=str)
        if isinstance(text, bytes):
            text = text.decode()
        self.assertNotIn(SENTINEL, text)
