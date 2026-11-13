"""The steps part: a list of steps or checks, each with its icon, or numbered
when they are done in order."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import elements, with_class


def _render(source: str) -> str:
    return Template(source).render(Context())


class StepsTagTests(SimpleTestCase):
    def test_numbered_steps_are_an_ordered_list_whose_steps_draw_their_number_and_no_icon(self):
        html = _render(
            "{% steps numbered=True %}{% step %}<strong>Pull</strong>{% endstep %}"
            "{% step %}<strong>Run</strong>{% endstep %}{% endsteps %}"
        )

        (listed,) = elements(html, "ol")
        self.assertIn("is-numbered", listed.classes)
        self.assertEqual([step.words for step in elements(html, "li")], ["Pull", "Run"])
        self.assertEqual(with_class(html, "band-icon"), [])

    def test_steps_not_numbered_each_draw_their_icon(self):
        html = _render('{% steps %}{% step icon="card" %}<strong>Register</strong>{% endstep %}{% endsteps %}')

        (listed,) = elements(html, "ol")
        self.assertNotIn("is-numbered", listed.classes)
        (step,) = elements(html, "li")
        (icon,) = with_class(html, "band-icon")
        self.assertIn(step, icon.ancestors)
        self.assertEqual(step.words, "Register")
