"""A part of a reading refused while the rest read is never shown as fully
readable. With redirect rules refused on every zone and page rules read, the
stored marker record per zone must not count as a record, read as "Readable",
or let the apex's path say the edge answers with nothing unread.
"""

from __future__ import annotations

from ipaddress import ip_network
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from hq.domains.control_plane.reading_parts import clean_refused_parts, parts_of

from ..credential_sight import PARTIAL, credential_sight
from ..facts import UNREADABLE, facts_about, unreadable_labels
from ..findings import derive_findings
from ..inventory import inventory_state, record_inventory
from ..paths import path_to
from ..projection import projection_scope
from ..security import cli_principal
from ..topology import derive_topology

ACCOUNT = "0123abcd"
REDIRECT = "cloudflare.redirect"
AUTH = "Cloudflare refused the request: Authentication error"
MISSING = "Single Redirect Read (zone)"
PHRASE = f"Redirect rules not read: missing {MISSING}"


def refused(zone, part="rules", refusal="permission"):
    return {"part": part, "refusal": refusal, "reason": AUTH, "scope": zone,
            "connection_ref": "example-api"}


def sweep(**kinds):
    return record_inventory(kinds, principal=cli_principal(), controller_id="example-controller")


def refused_on_every_zone():
    """Page rules read (none); redirect rules refused on both zones."""

    return sweep(**{
        REDIRECT: {"ok": True, "records": [],
                   "refused_parts": [refused("example.com"), refused("example.net")]},
        "cloudflare.pages_project": {"ok": True, "records": [
            {"name": "site", "account_id": ACCOUNT, "connection_ref": "example-api"}]},
    })


@mock.patch("hq.platform.application.reach.DOCUMENTATION", (ip_network("192.0.2.0/24"),))
class PartlyRefusedReadingTests(TestCase):
    def setUp(self):
        for zone in ("example.com", "example.net"):
            ManagedResource.objects.create(
                key=f"{zone.replace('.', '-')}-zone", kind="cloudflare.zone",
                spec={"zone": zone, "connection_ref": "example-api"},
            )
        ProviderInventory.objects.create(
            kind="cloudflare.dns_record",
            records=[{"zone": "example.net", "name": "example.net", "record_type": "A",
                      "content": "192.0.2.1", "proxied": True, "ttl": 1,
                      "connection_ref": "example-dns"}],
            observed_at=timezone.now(),
        )
        ProviderConnection.objects.create(
            connection_ref="example-api", controller_id="example-controller",
            provider="cloudflare_api", observed_at=timezone.now(),
            store={"vault": "Example Vault", "item": "exampleitem01"},
        )
        self.summary = refused_on_every_zone()

    def sight(self):
        (found,) = [item for item in credential_sight() if item.provider == "cloudflare_api"]
        return found, next(item for item in found.sights if item.kind == REDIRECT)

    def test_the_sight_is_partly_refused_and_names_the_permission(self):
        provider, seen = self.sight()

        self.assertEqual(seen.state, PARTIAL)
        self.assertEqual(seen.state_label, "Partly refused")
        self.assertEqual(seen.records, 0)
        self.assertEqual(seen.missing, (MISSING,))
        self.assertIn(MISSING, provider.missing)
        self.assertIn("Redirect rules", provider.unseen)

    def test_the_sweep_summary_and_provider_readings_say_partly_refused(self):
        reported = self.summary["kinds"][REDIRECT]

        self.assertEqual((reported["label"], reported["records"]), ("Partly refused", 0))
        self.assertEqual(reported["refused_parts"], [PHRASE, PHRASE])
        state = {item["kind"]: item for item in inventory_state()}[REDIRECT]
        self.assertEqual((state["state_label"], state["count"]), ("Partly refused", 0))

    def test_the_names_facts_say_the_part_was_not_read(self):
        with projection_scope():
            facts = facts_about(("example.net",), ())
            labels = unreadable_labels()

        unread = [fact for fact in facts if fact.state == UNREADABLE]
        self.assertIn(("Redirect rules", PHRASE), [(fact.label, fact.detail) for fact in unread])
        self.assertIn("Redirect rules", labels)

    def test_the_path_never_says_the_edge_answers_without_saying_what_was_not_read(self):
        with projection_scope():
            path = path_to("example.net")

        # Named for the edge that answers, which is what "runs on" asks.
        edge = next(hop for hop in path.routes[0].hops if hop.step == "edge")
        self.assertEqual(path.ends_at.label, edge.name)
        self.assertTrue(path.ends_at.label)
        self.assertEqual(path.ends_at.unread, PHRASE)
        self.assertIn(PHRASE, path.gaps)

    def test_the_missing_permission_is_a_finding_with_the_mint(self):
        principal = cli_principal()
        with projection_scope():
            found = {
                finding.rule: finding
                for finding in derive_findings(derive_topology(principal=principal),
                                               principal=principal)
            }

        finding = found["credential-missing-permissions"]
        self.assertIn(("Missing", MISSING), finding.evidence)
        self.assertIn(f"--account {ACCOUNT}", finding.steps[0].command)

    def test_a_refusal_on_one_zone_leaves_the_other_readable(self):
        sweep(**{REDIRECT: {"ok": True, "records": [], "refused_parts": [refused("example.com")]}})

        with projection_scope():
            path = path_to("example.net")

        self.assertNotIn(PHRASE, path.gaps)


class NoConnectionTests(TestCase):
    def test_a_kind_no_connection_reads_says_not_connected(self):
        summary = sweep(**{"cloudflare.tunnel": {"ok": True, "records": [], "connected": False}})

        self.assertEqual(
            summary["kinds"]["cloudflare.tunnel"],
            {"state": "not_connected", "label": "Not connected", "records": None,
             "refused_parts": []},
        )


class CleanTests(TestCase):
    def test_only_declared_parts_and_known_refusals_are_kept(self):
        cleaned = clean_refused_parts(REDIRECT, [
            refused("Example.COM"),
            {"part": "gremlins", "refusal": "permission"},
            {"part": "page_rules", "refusal": "sideways", "reason": "x" * 999},
            "not a mapping",
        ])

        self.assertEqual([entry["part"] for entry in cleaned], ["rules", "page_rules"])
        self.assertEqual(cleaned[0]["scope"], "example.com")
        self.assertEqual(cleaned[1]["refusal"], "")
        self.assertEqual(len(cleaned[1]["reason"]), 300)

    def test_a_machine_scope_keeps_its_address_only_when_it_is_one(self):
        cleaned = clean_refused_parts("portainer.image", [
            {"part": "", "scope": "edge-2", "address": " 198.51.100.30 "},
            {"part": "", "scope": "edge-3", "address": "not-an-address"},
        ])

        self.assertEqual(cleaned[0]["address"], "198.51.100.30")
        self.assertNotIn("address", cleaned[1])
        self.assertNotIn("address", clean_refused_parts(REDIRECT, [refused("example.com")])[0])

    def test_a_refused_read_keeps_no_parts(self):
        sweep(**{REDIRECT: {"ok": False, "records": [], "error": AUTH,
                            "refused_parts": [refused("example.com")]}})

        self.assertEqual(ProviderInventory.objects.get(kind=REDIRECT).refused_parts, [])

    def test_every_kind_has_its_whole_part_and_declared_parts(self):
        self.assertEqual(list(parts_of(REDIRECT)), ["", "rules", "page_rules"])
        self.assertEqual(list(parts_of("cloudflare.zone")), ["", "posture", "registration"])
