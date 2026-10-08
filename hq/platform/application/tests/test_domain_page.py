"""A domain's page says each thing once, in the owner's words."""

from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.domains.control_plane.zone_views import _page_relationships, _records_lede
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.relationships import RelationGroup, Relationship, Relationships


def group(phrase: str, *ends: EntityLink) -> RelationGroup:
    return RelationGroup(phrase, 0, tuple(Relationship(end) for end in ends))


SERVICE = EntityLink("app.example.com", "/services/app.example.com/", kind="service")
CERTIFICATE = EntityLink("example.com", "/records/example-cert/", kind="resource")
CONNECTION = EntityLink("example-dns", "/connections/#example-dns", kind="connection")


def zone(**fields) -> SimpleNamespace:
    card = SimpleNamespace(rows=(SimpleNamespace(url=SERVICE.url),), links=())
    return SimpleNamespace(cards=(card,), **fields)


class RelationsSaidOnceTests(SimpleTestCase):
    def shown(self):
        return _page_relationships(
            zone(),
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
        bare = SimpleNamespace(cards=())
        relationships, _ = _page_relationships(
            bare, Relationships("zone:example.com", groups=(group("Contains", SERVICE),))
        )

        self.assertEqual(len(relationships.groups), 1)


class RecordsLedeTests(SimpleTestCase):
    def test_one_record_is_one_record(self):
        one = SimpleNamespace(managed=True, adoptable=(), managed_count=1, records=(object(),))
        many = SimpleNamespace(managed=True, adoptable=(), managed_count=17, records=())
        unmanaged = SimpleNamespace(managed=False, adoptable=(), managed_count=0, records=(1, 2))

        self.assertEqual(_records_lede(one), "1 record, all managed by HQ")
        self.assertEqual(_records_lede(many), "17 records, all managed by HQ")
        self.assertEqual(_records_lede(unmanaged), "2 records, none managed by HQ")


class RecordTableTests(SimpleTestCase):
    def record(self, **fields):
        base = dict(
            name="app.example.com", record_type="A", value="192.0.2.10", proxied=False, ttl=1,
            service_url="", manageable=True, managed=True, edit_url="/edit/", remove_url="/remove/",
            health={"state": "healthy", "label": "Healthy"},
        )
        return SimpleNamespace(**{**base, **fields})

    def table(self, *rows, **context):
        return render_to_string(
            "control_plane/_zone_records.html",
            {"rows": rows, "zone": SimpleNamespace(managed=True), "public_dns_enabled": True,
             "request": SimpleNamespace(get_full_path=lambda: "/domains/example.com/"), **context},
        )

    def test_a_state_is_said_only_where_it_is_not_healthy(self):
        html = self.table(
            self.record(),
            self.record(name="old.example.com", health={"state": "degraded", "label": "Needs attention"}),
        )

        self.assertNotIn("Healthy", html)
        self.assertEqual(html.count("Needs attention"), 1)

    def test_a_ttl_is_said_with_its_unit_and_only_when_it_was_set(self):
        html = self.table(self.record(), self.record(name="slow.example.com", ttl=300))

        self.assertNotIn("Auto", html)
        self.assertIn("<td>300 s</td>", html)

    def test_an_empty_half_says_where_the_records_are(self):
        self.assertIn("No records.", self.table())
        self.assertIn("They are all below.", self.table(empty_message="They are all below."))
