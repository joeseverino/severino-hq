"""A credential's missing permissions, expiry and mint command, derived."""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.credential_reads import observer_permissions
from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters.contracts import CREDENTIAL_REFUSAL
from hq.platform.core.testing import operator
from tests.mise_tasks import body, mise_tasks

from ..credential_mint import (
    projection_field,
)
from ..report_testing import report_connections
from .commands import commands_on

ACCOUNT = "0123abcd"
STORE = {
    "vault": "Example Vault",
    "item": "exampleitem01",
    "bootstrap": "op://Operator Vault/Cloudflare bootstrap",
}
SECRET = "sekret-value-that-must-not-appear"
PAGES = "cloudflare.pages_project"


def cloudflare_kinds() -> tuple[str, ...]:
    return tuple(kind for kind, spec in OBSERVATIONS.items() if spec.provider == "cloudflare_api")


def production_shape(refusal: str = CREDENTIAL_REFUSAL) -> None:
    """Pages read; every other account reading refused with code 10000.

    What an older controller stored: each refusal marked as the credential.
    """

    now = timezone.now()
    for kind in cloudflare_kinds():
        if kind == PAGES:
            ProviderInventory.objects.create(
                kind=kind,
                records=[{"name": "example", "account_id": ACCOUNT}],
                observed_at=now,
            )
        else:
            ProviderInventory.objects.create(
                kind=kind,
                reachable=False,
                refusal=refusal,
                error="Authentication error",
                observed_at=now,
            )


@tag("source")
class MinterTaskTests(SimpleTestCase):
    """Each minter against the task that mints it: the command HQ prints runs as printed."""

    def test_every_minter_matches_its_task(self):
        tasks = mise_tasks()
        source = (Path(settings.BASE_DIR) / "tools" / "credentials.py").read_text()
        minters = {provider: kind.minter for provider, kind in CONNECTION_KINDS.items() if kind.minter}
        self.assertTrue(minters)
        for provider, minter in minters.items():
            with self.subTest(provider=provider):
                flags = set(re.findall(r'^#USAGE flag "(--[a-z-]+)', body(tasks[minter.task]), flags=re.MULTILINE))
                for flag, variable in minter.stores:
                    self.assertIn(flag, flags)
                    projection_field(variable)
                for name, variable in minter.bootstrap:
                    self.assertIn(f'"{name}"', source)
                    projection_field(variable)
                if minter.account_flag:
                    self.assertIn(minter.account_flag, flags)
                self.assertTrue(observer_permissions(provider))


class RenderedFixTests(TestCase):
    def setUp(self):
        operator(self.client)
        production_shape()
        report_connections(
            [
                {
                    "connection_ref": "example-api",
                    "provider": "cloudflare_api",
                    "store": {**STORE, "API_TOKEN": SECRET},
                    "expires_at": (timezone.now() + timedelta(days=5)).isoformat(),
                }
            ],
            controller_id="example-controller",
        )

    def test_the_connections_page_offers_the_command_and_no_secret(self):
        response = self.client.get(reverse("control_plane:connections"))

        self.assertIn("--account 0123abcd", commands_on(response))
        self.assertIn("op://Example Vault/exampleitem01/credential", commands_on(response))
        self.assertNotContains(response, SECRET)
