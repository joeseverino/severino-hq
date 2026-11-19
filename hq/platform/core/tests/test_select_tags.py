"""The select: a choice of one is one part, which may say its choice in a tone."""

import re

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, tag

from hq.platform.application.markup_testing import elements
from hq.platform.core.tests.test_part_tags import _catalog_templates

OPTIONS = (("", "Default"), ("allow", "On"), ("deny", "Off"))


def _render(source: str, **context) -> str:
    return Template(source).render(Context({"options": OPTIONS, **context}))


class SelectTagTests(SimpleTestCase):
    def test_it_posts_its_name_with_an_option_for_each_choice_the_chosen_one_selected(self):
        html = _render('{% select "rule" options chosen="deny" %}')

        (field,) = elements(html, "select")
        self.assertEqual(field.attrs["name"], "rule")
        options = elements(html, "option")
        self.assertEqual(
            [(o.attrs["value"], o.words) for o in options], [("", "Default"), ("allow", "On"), ("deny", "Off")]
        )
        self.assertEqual([o.attrs["value"] for o in options if "selected" in o.attrs], ["deny"])

    def test_with_nothing_chosen_the_option_with_no_value_is(self):
        html = _render('{% select "rule" options chosen=rule %}', rule=None)

        self.assertEqual([o.attrs["value"] for o in elements(html, "option") if "selected" in o.attrs], [""])

    def test_a_value_alone_is_its_own_words_and_a_number_is_chosen_by_its_value(self):
        html = _render('{% select "port" ports chosen="443" %}', ports=(80, 443))

        options = elements(html, "option")
        self.assertEqual([(o.attrs["value"], o.words) for o in options], [("80", "80"), ("443", "443")])
        self.assertEqual([o.attrs["value"] for o in options if "selected" in o.attrs], ["443"])

    def test_a_choice_that_narrows_leads_with_any_of_its_dimension_chosen_by_default(self):
        html = _render('{% select "kind" kinds chosen=kind every="type" %}', kinds=(("host", "Host"),), kind=None)

        options = elements(html, "option")
        self.assertEqual([(o.attrs["value"], o.words) for o in options], [("", "Any type"), ("host", "Host")])
        self.assertIn("selected", options[0].attrs)

    def test_a_choice_that_names_its_own_default_is_a_pick_and_never_any(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% select "view" options every="view" %}')

    def test_a_tone_outside_the_status_vocabulary_and_a_select_with_no_name_are_refused(self):
        for source in ('{% select "rule" options tone="allow" %}', '{% select "" options %}'):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                _render(source)


@tag("source")
class SelectCatalogTests(SimpleTestCase):
    def test_the_catalog_draws_a_select_in_a_tone_and_one_a_control_label_names(self):
        """The browser survey reads the part through the catalog, so each form
        it takes is drawn there at least once."""

        drawn = re.findall(r"\{% select (.*?)%\}", " ".join(_catalog_templates().values()))
        self.assertTrue(any("id=" in found for found in drawn), "draw a select a control label names")
        self.assertTrue(any("tone=" in found for found in drawn), "draw a select in a tone")
        self.assertTrue(any("every=" in found for found in drawn), "draw a select that narrows")
