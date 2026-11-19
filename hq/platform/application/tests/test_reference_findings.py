"""The links the database derives from a declaration, and what HQ says of one
that names nothing."""

from django.db import connection
from django.test import SimpleTestCase, TestCase, tag

from hq.domains.control_plane.models import REFERENCE_COLUMNS, ManagedResource
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.testing import declare

from .. import reference_findings as rules
from .test_tailnet_posture import raised

ZONE, RECORD, TARGET = "cloudflare.zone", "cloudflare.dns_record", "tls.delivery_target"


def record(key: str, zone: str, **spec) -> ManagedResource:
    name = f"app.{normalized_hostname(zone)}"
    return declare(key, RECORD, {"zone": zone, "name": name, "record_type": "A", "content": "192.0.2.10", **spec})


def target(key: str, ref: str) -> ManagedResource:
    return declare(key, TARGET, {"kind": "caddy", "connection_ref": ref, "name": key})


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
            made = declare(key, "machine", spec)
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


class NamesNoConnectionTests(TestCase):
    def test_nothing_is_said_before_a_controller_reports_its_connections(self):
        target("on-nothing", "example-edge-2")

        self.assertEqual(raised(rules.RULE), [])


@tag("source")
class RuleSourceTests(SimpleTestCase):
    """The rule's source names no kind of its own."""

    def test_the_rule_names_no_kind_of_its_own(self):
        """The relations come from the registry's ``contains``; a second one is covered when declared."""

        from pathlib import Path

        source = Path(rules.__file__).read_text(encoding="utf-8")
        for kind in PROVIDERS:
            self.assertNotIn(f'"{kind}"', source)
