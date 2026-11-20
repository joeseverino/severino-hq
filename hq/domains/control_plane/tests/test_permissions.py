"""Every permission HQ asks an operator for comes from its provider's table,
which GitHub's registration of HQ's App grants."""

import json
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]


def asked(provider: str) -> tuple[str, ...]:
    """Every permission HQ names to an operator for one connection provider."""

    from ..connection_kinds import CONNECTION_KINDS
    from ..credential_reads import observer_permissions

    return (*observer_permissions(provider), *CONNECTION_KINDS[provider].writes)


class PermissionTableTests(SimpleTestCase):
    def test_each_provider_takes_every_name_from_one_table(self):
        from ..connection_kinds import CONNECTION_KINDS
        from ..permissions import Permission

        for provider in CONNECTION_KINDS:
            with self.subTest(provider=provider):
                tables = {type(name) for name in asked(provider)}
                self.assertLessEqual(len(tables), 1, tables)
                self.assertTrue(all(issubclass(table, Permission) for table in tables), tables)

    def test_cloudflare_shows_the_dashboard_name_beside_the_api_name(self):
        from ..permissions import Cloudflare

        self.assertEqual(Cloudflare.D1_EDIT.api, "D1 Write (account)")
        self.assertIn("D1 Edit", Cloudflare.D1_EDIT)
        self.assertIn("D1 Write", Cloudflare.D1_EDIT)
        self.assertEqual(Cloudflare.D1_READ, Cloudflare.D1_READ.api)

    @tag("source")
    def test_every_github_permission_is_one_the_app_is_registered_with(self):
        """GitHub refuses a token asking for more than the App declares."""

        declared = json.loads((ROOT / "deploy" / "github-apps.json").read_text())["apps"]["hq"]["permissions"]
        levels = ("read", "write")
        for name in asked("github_app"):
            with self.subTest(name=name):
                key, level = name.api.split(": ")
                self.assertIn(key, declared)
                self.assertGreaterEqual(levels.index(declared[key]), levels.index(level))
