"""A domain in the palette holds every hostname under it, by the one zone rule."""

from __future__ import annotations

from django.test import SimpleTestCase

from .command_center import DiscoveryItem, _matching_estate

ZONE = DiscoveryItem(
    kind="zone",
    name="example.com",
    label="example.com",
    summary="Domain",
    url="/domains/example.com/",
    destination_label="Domain",
    badges=(),
)


class HoldsNameTests(SimpleTestCase):
    def test_a_hostname_under_the_zone_finds_it_in_any_spelling(self):
        for query in ("app.example.com", "APP.Example.COM.", "*.app.example.com"):
            with self.subTest(query=query):
                self.assertEqual(_matching_estate((ZONE,), query), (ZONE,))

    def test_a_name_that_only_ends_in_the_zone_text_does_not(self):
        self.assertEqual(_matching_estate((ZONE,), "notexample.com"), ())
