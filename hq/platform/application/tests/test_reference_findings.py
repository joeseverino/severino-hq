"""The links the database derives from a declaration, and what HQ says of one
that names nothing."""

from __future__ import annotations

from django.db import connection
from django.test import TestCase

from hq.domains.control_plane.models import REFERENCE_COLUMNS, ManagedResource
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS

from .. import reference_findings as rules
from ..inventory_testing import store
from .test_tailnet_posture import raised, tailnet_connection

ZONE, RECORD, TARGET = "cloudflare.zone", "cloudflare.dns_record", "tls.delivery_target"


def record(key: str, zone: str, **spec) -> ManagedResource:
    name = f"app.{normalized_hostname(zone)}"
    return ManagedResource.objects.create(
        key=key,
        kind=RECORD,
        spec={"zone": zone, "name": name, "record_type": "A", "content": "192.0.2.10", **spec},
    )


def target(key: str, ref: str) -> ManagedResource:
    return ManagedResource.objects.create(
        key=key, kind=TARGET, spec={"kind": "caddy", "connection_ref": ref, "name": key}
    )


class DerivedColumnTests(TestCase):
    def test_a_link_is_what_the_spec_says(self):
        made = record("example-record", "example.com", connection_ref="example-dns")
        made.refresh_from_db()
        self.assertEqual((made.zone, made.connection_ref), ("example.com", "example-dns"))
        self.assertEqual(list(ManagedResource.objects.filter(zone="example.com")), [made])
        self.assertEqual(list(ManagedResource.objects.filter(connection_ref="example-dns")), [made])

    def test_a_link_follows_its_spec(self):
        made = record("example-record", "example.com")
        made.spec = {**made.spec, "zone": "example.org"}
        made.save()
        self.assertEqual(made.zone, "example.org")
        self.assertFalse(ManagedResource.objects.filter(zone="example.com").exists())

    def test_a_spec_with_no_link_derives_none(self):
        for key, spec in (("empty", {}), ("null", {"zone": None, "connection_ref": None})):
            made = ManagedResource.objects.create(key=key, kind="machine", spec=spec)
            made.refresh_from_db()
            self.assertEqual((made.zone, made.connection_ref), ("", ""))

    def test_a_domain_is_spelled_one_way(self):
        """As ``normalized_hostname`` spells it, so a join needs no second rule."""

        for index, written in enumerate(
            ("Example.COM", " example.com ", "example.com.", "EXAMPLE.com. ", "sub.Example.com..")
        ):
            made = record(f"spelled-{index}", written)
            made.refresh_from_db()
            self.assertEqual(made.zone, normalized_hostname(written), written)
        spaced = target("spaced", " example-edge ")
        spaced.refresh_from_db()
        self.assertEqual(spaced.connection_ref, "example-edge")

    def test_the_links_are_indexed_columns_the_database_computes(self):
        table = ManagedResource._meta.db_table
        with connection.cursor() as cursor:
            indexed = {
                tuple(details["columns"])
                for details in connection.introspection.get_constraints(cursor, table).values()
                if details["index"]
            }
        for column in REFERENCE_COLUMNS:
            field = ManagedResource._meta.get_field(column)
            self.assertTrue(field.generated and field.db_persist, column)
            self.assertIn((column,), indexed)

    def test_every_declared_containment_is_a_derived_link(self):
        found = rules.containments()
        self.assertIn((ZONE, RECORD, "zone", "zone"), found)
        for _holder, _held, held_field, _holder_field in found:
            self.assertIn(held_field, REFERENCE_COLUMNS)


class NamesNoDomainTests(TestCase):
    def setUp(self):
        tailnet_connection("example-dns", "cloudflare_dns")

    def read(self, *zones: str, **fields) -> None:
        store(ZONE, *({"zone": zone, "connection_ref": "example-dns"} for zone in zones), **fields)

    def test_a_record_in_a_domain_hq_does_not_have(self):
        self.read("example.com")
        record("in-a-domain", "example.com")
        record("in-no-domain", "typo.example")

        (finding,) = raised(rules.RULE)

        self.assertEqual(finding["subject"], "resource:in-no-domain")
        # The record is called what every page calls it.
        self.assertEqual(
            finding["title"], "app.typo.example → 192.0.2.10 is in typo.example, which HQ does not have"
        )
        self.assertEqual(
            finding["explanation"],
            "HQ expects no domain called typo.example and no connection reads one, so "
            "app.typo.example → 192.0.2.10 cannot be applied anywhere.",
        )
        self.assertEqual(finding["severity"], "attention")
        self.assertEqual([remedy["label"] for remedy in finding["remedies"]], ["Change what HQ expects"])

    def test_a_domain_hq_expects_is_there_whether_or_not_it_was_read(self):
        self.read("example.com")
        ManagedResource.objects.create(key="expected", kind=ZONE, spec={"zone": "Example.ORG"})
        record("in-an-expected-domain", "example.org")

        self.assertEqual(raised(rules.RULE), [])

    def test_a_domain_spelled_another_way_is_the_same_domain(self):
        self.read("example.com")
        record("spelled-differently", "Example.COM.")

        self.assertEqual(raised(rules.RULE), [])

    def test_nothing_is_said_without_a_list_of_the_domains(self):
        record("in-no-domain", "typo.example")
        self.assertEqual(raised(rules.RULE), [])

        self.read("example.com", reachable=False, error="The read failed.")
        self.assertEqual(raised(rules.RULE), [])

    def test_a_record_that_is_switched_off_is_not_reported(self):
        self.read("example.com")
        made = record("in-no-domain", "typo.example")
        made.enabled = False
        made.save()

        self.assertEqual(raised(rules.RULE), [])


class NamesNoConnectionTests(TestCase):
    def test_a_record_that_uses_a_connection_no_controller_has(self):
        tailnet_connection("example-edge", "ssh")
        target("on-the-edge", "example-edge")
        target("on-nothing", "example-edge-2")

        (finding,) = raised(rules.RULE)

        self.assertEqual(finding["subject"], "resource:on-nothing")
        self.assertEqual(finding["title"], "on-nothing uses a connection no controller has")
        self.assertEqual(
            finding["explanation"],
            "It names the connection example-edge-2. No controller has one called that, so "
            "nothing reads on-nothing and nothing applies HQ's settings to it.",
        )
        self.assertIn({"label": "Connection it names", "value": "example-edge-2"}, finding["evidence"])
        self.assertEqual([remedy["label"] for remedy in finding["remedies"]], ["Change what HQ expects"])

    def test_nothing_is_said_before_a_controller_reports_its_connections(self):
        target("on-nothing", "example-edge-2")

        self.assertEqual(raised(rules.RULE), [])

    def test_a_record_that_names_no_connection_is_not_reported(self):
        tailnet_connection("example-edge", "ssh")
        ManagedResource.objects.create(key="unconnected", kind="machine", spec={"name": "example"})

        self.assertEqual(raised(rules.RULE), [])

    def test_both_links_of_one_record_are_reported(self):
        tailnet_connection("example-dns", "cloudflare_dns")
        store(ZONE, {"zone": "example.com", "connection_ref": "example-dns"})
        record("lost", "typo.example", connection_ref="example-other")

        self.assertEqual(
            sorted(finding["title"] for finding in raised(rules.RULE)),
            [
                "app.typo.example → 192.0.2.10 is in typo.example, which HQ does not have",
                "app.typo.example → 192.0.2.10 uses a connection no controller has",
            ],
        )


class RuleTests(TestCase):
    def test_checking_the_links_asks_the_database_nothing_more(self):
        """The declarations and readings are in hand when the estate is built."""

        from ..infrastructure import enabled_resources
        from ..projection import projection_scope
        from ..topology import relation_graph
        from .test_tailnet_posture import EVERYTHING

        tailnet_connection("example-dns", "cloudflare_dns")
        store(ZONE, {"zone": "example.com", "connection_ref": "example-dns"})
        record("lost", "typo.example", connection_ref="example-other")
        with projection_scope():
            graph = relation_graph(principal=EVERYTHING)
            nodes = {node.id: node for node in graph.topology.nodes}
            with self.assertNumQueries(0):
                rules.add(nodes, enabled_resources())
        self.assertEqual(
            {key for key, _value in nodes["resource:lost"].facts},
            {rules.NO_CONNECTION, rules.NO_PARENT},
        )

    def test_the_rule_names_no_kind_of_its_own(self):
        """The relations come from the registry's ``contains``; a second one is covered when declared."""

        from pathlib import Path

        source = Path(rules.__file__).read_text(encoding="utf-8")
        for kind in PROVIDERS:
            self.assertNotIn(f'"{kind}"', source)
