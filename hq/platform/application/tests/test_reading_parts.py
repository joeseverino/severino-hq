"""A part of a reading refused while the rest read is never shown as fully
readable. With redirect rules refused on every zone and page rules read, the
stored marker record per zone must not count as a record, read as "Readable",
or let the apex's path say the edge answers with nothing unread.
"""

from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.permissions import Cloudflare
from hq.domains.control_plane.reading_parts import clean_refused_parts
from hq.platform.core.testing import connection, declare

from ..connection_context import connections_context
from ..credential_mint import credential_fixes
from ..inventory import record_inventory
from ..projection import projection_scope
from ..report_testing import refused_part
from ..security import cli_principal

ACCOUNT = "0123abcd"
REDIRECT = "cloudflare.redirect"
AUTH = "Cloudflare refused the request: Authentication error"
MISSING = Cloudflare.SINGLE_REDIRECT_READ


def refused(zone, part="rules", refusal="permission"):
    return {"part": part, "refusal": refusal, "reason": AUTH, "scope": zone, "connection_ref": "example-api"}


def sweep(**kinds):
    return record_inventory(kinds, controller_id="example-controller")


def refused_on_every_zone():
    """Page rules read (none); redirect rules refused on both zones."""

    return sweep(
        **{
            REDIRECT: {"ok": True, "records": [], "refused_parts": [refused("example.com"), refused("example.net")]},
            "cloudflare.pages_project": {
                "ok": True,
                "records": [{"name": "site", "account_id": ACCOUNT, "connection_ref": "example-api"}],
            },
        }
    )


@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class PartlyRefusedReadingTests(TestCase):
    def setUp(self):
        for zone in ("example.com", "example.net"):
            declare(
                f"{zone.replace('.', '-')}-zone", "cloudflare.zone", {"zone": zone, "connection_ref": "example-api"}
            )
        ProviderInventory.objects.create(
            kind="cloudflare.dns_record",
            records=[
                {
                    "zone": "example.net",
                    "name": "example.net",
                    "record_type": "A",
                    "content": "192.0.2.1",
                    "proxied": True,
                    "ttl": 1,
                    "connection_ref": "example-dns",
                }
            ],
            observed_at=timezone.now(),
        )
        connection("cloudflare_api", "example-api", store={"vault": "Example Vault", "item": "exampleitem01"})
        refused_on_every_zone()

    def test_a_part_refused_to_one_connection_is_missing_from_that_one_alone(self):
        connection("cloudflare_api", "example-other")

        with projection_scope():
            fixes = credential_fixes()
            rows = {row.instance.connection_ref: row for row in connections_context(principal=cli_principal()).rows}

        for ref, lacks in (("example-api", (MISSING,)), ("example-other", ())):
            redirect = next(state for state in rows[ref].connection.abilities if state.ability.name == REDIRECT)
            self.assertEqual((fixes[ref].missing, redirect.missing_scopes), (lacks, lacks), ref)


class CleanTests(TestCase):
    def test_only_declared_parts_are_kept_with_bounded_text(self):
        cleaned = clean_refused_parts(
            REDIRECT,
            [
                refused("Example.COM"),
                # A part the contract names and this kind does not declare.
                refused_part("querylog", refusal="permission"),
                refused_part("page_rules", reason="x" * 999),
            ],
        )

        self.assertEqual([entry["part"] for entry in cleaned], ["rules", "page_rules"])
        self.assertEqual(cleaned[0]["scope"], "example.com")
        self.assertLess(len(cleaned[1]["reason"]), 999)

    def test_a_machine_scope_keeps_its_address_only_when_it_is_one(self):
        cleaned = clean_refused_parts(
            "portainer.image",
            [
                refused_part("", scope="edge-2", address=" 198.51.100.30 "),
                refused_part("", scope="edge-3", address="not-an-address"),
            ],
        )

        self.assertEqual(cleaned[0]["address"], "198.51.100.30")
        self.assertNotIn("address", cleaned[1])
        self.assertNotIn("address", clean_refused_parts(REDIRECT, [refused("example.com")])[0])
