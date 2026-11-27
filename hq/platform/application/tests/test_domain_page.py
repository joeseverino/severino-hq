"""A domain's page says each thing once, in the owner's words."""

from django.test import SimpleTestCase

from hq.domains.control_plane.zone_views import _page_relationships
from hq.platform.application.entity_links import EntityLink
from hq.platform.application.relationships import Relationships
from hq.platform.application.topology_model import RelationGroup, Relationship


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

    def test_the_readings_as_they_arrived_are_not_page_content(self):
        relationships, _read_with = self.shown()

        self.assertEqual(relationships.readouts, ())
