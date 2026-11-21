"""A domain's page says each thing once, in the owner's words."""

from django.test import SimpleTestCase

from hq.domains.control_plane.zone_views import _page_relationships, _records_lede, records_table
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.infrastructure import ResourceState
from hq.platform.application.markup_testing import elements, select, text, texts, with_class
from hq.platform.application.relationships import Relationships
from hq.platform.application.topology_model import RelationGroup, Relationship
from hq.platform.application.zones import ZoneRecord, told_apart
from hq.platform.core.testing import render


def group(phrase: str, *ends: EntityLink) -> RelationGroup:
    return RelationGroup(phrase, 0, tuple(Relationship(end) for end in ends))


SERVICE = EntityLink("app.example.com", "/services/app.example.com/", kind="service")
CERTIFICATE = EntityLink("example.com", "/records/example-cert/", kind="resource")
CONNECTION = EntityLink("example-dns", "/connections/#example-dns", kind="connection")


def domain(*insights) -> dict:
    return {"insights": list(insights)}


CARD = {"notice": False, "rows": [{"url": SERVICE.url}], "links": []}


class RelationsSaidOnceTests(SimpleTestCase):
    def shown(self):
        return _page_relationships(
            domain(CARD),
            Relationships(
                "zone:example.com",
                groups=(
                    group("Contains", SERVICE),
                    group("Covered by certificate", CERTIFICATE),
                    group("Reached through", CONNECTION),
                ),
                readouts=(("Example reading", None, '{"account": "example"}'),),
            ),
        )

    def test_what_a_card_already_names_is_not_listed_again(self):
        relationships, _read_with = self.shown()

        self.assertEqual([found.phrase for found in relationships.groups], ["Covered by certificate"])

    def test_what_the_domain_is_read_with_is_named_apart(self):
        relationships, read_with = self.shown()

        self.assertEqual(read_with, (CONNECTION,))
        self.assertNotIn("Reached through", [found.phrase for found in relationships.groups])

    def test_the_readings_as_they_arrived_are_not_page_content(self):
        relationships, _read_with = self.shown()

        self.assertEqual(relationships.readouts, ())

    def test_a_relation_no_card_names_stays(self):
        relationships, _ = _page_relationships(
            domain(), Relationships("zone:example.com", groups=(group("Contains", SERVICE),))
        )

        self.assertEqual(len(relationships.groups), 1)


class RecordsLedeTests(SimpleTestCase):
    def test_one_record_is_one_record(self):
        managed = {"declaration": "example-record", "manageable": True, "observed_only": False}
        one = {"declaration": "example-zone", "records": [managed]}
        many = {"declaration": "example-zone", "records": [managed] * 17}
        unmanaged = {"declaration": "", "records": [{**managed, "declaration": ""}] * 2}

        self.assertEqual(_records_lede(one), "1 record, all managed by HQ")
        self.assertEqual(_records_lede(many), "17 records, all managed by HQ")
        self.assertEqual(_records_lede(unmanaged), "2 records, none managed by HQ")


class RecordTableTests(SimpleTestCase):
    def record(self, **fields):
        base = {
            "name": "app.example.com",
            "record_type": "A",
            "value": "192.0.2.10",
            "proxied": False,
            "ttl": 1,
            "service": "",
            "manageable": True,
            "declaration": "example-record",
            "health": {"state": ResourceState.HEALTHY, "label": "Healthy"},
        }
        return {**base, **fields}

    def table(self, *rows, policy=()):
        table = records_table([(row, ()) for row in rows], [(row, ()) for row in policy], managed=True)
        return render("{% data_table table %}", table=table)

    def test_a_state_is_said_only_where_it_is_not_healthy(self):
        html = self.table(
            self.record(),
            self.record(name="old.example.com", health={"state": ResourceState.DEGRADED, "label": "Needs attention"}),
        )

        self.assertNotIn("Healthy", html)
        self.assertEqual(text(html).count("Needs attention"), 1)

    def test_a_ttl_is_said_with_its_unit_and_only_when_it_was_set(self):
        html = self.table(self.record(), self.record(name="slow.example.com", ttl=300))

        self.assertNotIn("Auto", text(html))
        self.assertIn("300 s", texts(html, "td"))

    def test_a_zone_whose_every_ttl_is_automatic_has_no_ttl_column(self):
        html = self.table(self.record(), self.record(name="www.example.com"))

        self.assertNotIn("TTL", texts(html, "th"))

    def test_a_zone_with_no_records_says_so(self):
        self.assertIn("No records.", self.table())

    def test_policy_records_are_a_group_of_the_same_table_folded_away(self):
        html = self.table(self.record(), policy=(self.record(name="example.com", record_type="CAA"),))

        self.assertEqual(len(elements(html, "table")), 1)
        (fold,) = select(html, "data-folds")
        folded = select(html, "data-folded")
        self.assertEqual([row.attrs["data-folded"] for row in folded], [fold.attrs["id"]])
        self.assertIn("example.com", folded[0].words)

    def test_two_records_of_one_name_and_type_say_what_tells_them_apart_where_their_value_has_left(self):
        records = (
            ZoneRecord("example.com", "MX", "mx1.example.net", priority=10),
            ZoneRecord("example.com", "MX", "mx2.example.net", priority=20),
            ZoneRecord("mail.example.com", "MX", "mx1.example.net", priority=10),
        )
        html = self.table(
            *(
                self.record(name=each.name, record_type=each.record_type, value=each.content, summary=each.summary)
                for each in told_apart(records)
            )
        )

        said = {row.words for row in with_class(html, "narrow-only")}
        self.assertEqual(said, {"10 mx1.example.net", "20 mx2.example.net"})
