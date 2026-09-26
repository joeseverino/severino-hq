"""The minting scripts' permission lists match what the readers need.

Derived from ``OBSERVATIONS`` at test time, so a reading registered with a new
``requires`` fails here until the checked-in list names it.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from .credential_reads import UNREGISTERED_READS, observer_permissions
from .observations import OBSERVATIONS
from .providers import CONNECTION_CREDENTIALS

SCRIPTS = Path(settings.BASE_DIR) / "scripts"


def listed(name: str) -> tuple[str, ...]:
    lines = (SCRIPTS / name).read_text(encoding="utf-8").splitlines()
    return tuple(
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
    )


class ObserverPermissionFileTests(SimpleTestCase):
    def assert_matches(self, provider: str, filename: str) -> None:
        wanted = observer_permissions(provider)
        found = listed(filename)
        self.assertEqual(
            sorted(found),
            list(wanted),
            f"{filename} must list exactly the reads {provider} needs.",
        )
        self.assertEqual(len(found), len(set(found)), f"{filename} repeats a line.")

    def test_the_cloudflare_list_is_every_cloudflare_reading_and_reader(self):
        self.assert_matches("cloudflare_api", "cloudflare-observer-permissions.txt")

    def test_the_tailscale_list_is_every_tailscale_reading_and_reader(self):
        self.assert_matches("tailscale", "tailscale-observer-scopes.txt")

    def test_every_cloudflare_requires_names_its_scope(self):
        """The minting script resolves "<name> (<account|zone>)" and nothing else."""

        for name in observer_permissions("cloudflare_api"):
            self.assertRegex(name, r"^\S.* \((account|zone)\)$")

    def test_tailscale_reads_are_read_scopes(self):
        for name in observer_permissions("tailscale"):
            self.assertRegex(name, r"^[a-z_:]+:read$")

    def test_registered_requires_are_included(self):
        for spec in OBSERVATIONS.values():
            for name in spec.requires:
                self.assertIn(name, observer_permissions(spec.provider))

    def test_unregistered_reads_name_known_connection_providers(self):
        self.assertLessEqual(set(UNREGISTERED_READS), set(CONNECTION_CREDENTIALS))
