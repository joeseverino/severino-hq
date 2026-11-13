"""The select: a choice of one is one part, which may say its choice in a tone."""

import re

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import elements
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

    def test_its_label_is_the_name_a_reader_hears_and_its_id_the_one_a_control_label_names(self):
        named = _render('{% select "rule" options label=label %}', label='Edit a record, for "agent"')
        labeled = _render('{% select "rule" options id="example-rule" %}')

        (field,) = elements(named, "select")
        self.assertEqual(field.attrs["aria-label"], 'Edit a record, for "agent"')
        (field,) = elements(labeled, "select")
        self.assertEqual(field.attrs["id"], "example-rule")
        self.assertNotIn("aria-label", field.attrs)

    def test_a_tone_says_the_choice_in_the_status_part_s_ink(self):
        plain = _render('{% select "rule" options %}')
        toned = _render('{% select "rule" options tone="good" %}')

        self.assertEqual(elements(plain, "select")[0].classes, frozenset())
        self.assertLessEqual({"toned", "toned-good"}, elements(toned, "select")[0].classes)

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
