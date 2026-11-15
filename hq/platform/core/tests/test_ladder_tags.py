"""The check ladder: checks read down, each opening to why, are one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class CheckLadderTagTests(SimpleTestCase):
    def test_a_rung_says_its_state_statement_and_evidence_and_opens_to_its_body(self):
        html = _render(
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
        html = _render("{% check_ladder %}{% check_rung %}Example check{% endcheck_rung %}{% endcheck_ladder %}")

        self.assertNotIn("is-route", with_class(html, "check-ladder")[0].classes)
        self.assertEqual(with_class(html, "ladder-rung")[0].attrs["data-state"], "unavailable")
        self.assertNotIn("open", elements(html, "details")[0].attrs)
        for part in ("ladder-kind", "ladder-detail", "ladder-evidence", "ladder-more", "ladder-page", "ladder-body"):
            with self.subTest(part=part):
                self.assertEqual(with_class(html, part), [])

    def test_a_state_it_does_not_know_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% check_rung state="maybe" %}Example check{% endcheck_rung %}')

    def test_an_unknown_option_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% check_rung tone="good" %}Example check{% endcheck_rung %}')
