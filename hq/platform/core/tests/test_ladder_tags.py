"""The check ladder: checks read down, each opening to why, are one part."""

from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class
from hq.platform.core.testing import render


class CheckLadderTagTests(SimpleTestCase):
    def test_a_rung_says_its_state_statement_and_evidence_and_opens_to_its_body(self):
        html = render(
            "{% check_ladder route=True %}"
            '{% check_rung state="unmet" kind="Device" evidence=value open=True data_example="one" %}'
            "{{ said }}{% detail %}Example OS{% more %}2 checks{% page %}<a href='/example/'>Example</a>{% body %}<p>Why</p>{% endcheck_rung %}"
            "{% endcheck_ladder %}",
            said="<b>escaped</b>",
            value="192.0.2.10",
        )

        (ladder,) = with_class(html, "check-ladder")
        self.assertEqual(ladder.tag, "ol")
        self.assertIn("is-route", ladder.classes)
        (rung,) = with_class(html, "ladder-rung")
        self.assertEqual(rung.attrs["data-state"], "unmet")
        (disclosure,) = elements(html, "details")
        self.assertEqual(disclosure.attrs["data-example"], "one")
        self.assertIn("open", disclosure.attrs)
        (statement,) = with_class(html, "ladder-statement")
        self.assertTrue(statement.words.startswith("<b>escaped</b>"))
        self.assertEqual(with_class(html, "ladder-detail")[0].words, "Example OS")
        self.assertEqual(with_class(html, "ladder-evidence")[0].words, "192.0.2.10")
        self.assertEqual(with_class(html, "ladder-kind")[0].words, "Device")
        self.assertEqual(with_class(html, "ladder-more")[0].words, "2 checks")
        (body,) = with_class(html, "ladder-body")
        self.assertIn(disclosure, body.ancestors)
        (page,) = with_class(html, "ladder-page")
        self.assertIn(body, page.ancestors)

    def test_a_rung_given_no_state_was_not_read_and_draws_only_what_it_has(self):
        html = render("{% check_ladder %}{% check_rung %}Example check{% endcheck_rung %}{% endcheck_ladder %}")

        self.assertNotIn("is-route", with_class(html, "check-ladder")[0].classes)
        self.assertEqual(with_class(html, "ladder-rung")[0].attrs["data-state"], "unavailable")
        self.assertNotIn("open", elements(html, "details")[0].attrs)
        for part in ("ladder-kind", "ladder-detail", "ladder-evidence", "ladder-more", "ladder-page", "ladder-body"):
            with self.subTest(part=part):
                self.assertEqual(with_class(html, part), [])

    def test_a_stop_no_check_reads_draws_what_it_is_and_claims_no_standing(self):
        html = render(
            "{% check_ladder route=True %}"
            '{% check_rung icon="server" %}example-host-01{% endcheck_rung %}'
            '{% check_rung icon="server" state="met" %}example-host-02{% endcheck_rung %}'
            "{% endcheck_ladder %}"
        )

        unchecked, met = with_class(html, "ladder-rung")
        self.assertNotIn("data-state", unchecked.attrs)
        self.assertEqual([use.attrs["href"].rpartition("#")[2] for use in elements(html, "use")], ["server"])
        self.assertEqual(met.attrs["data-state"], "met")
