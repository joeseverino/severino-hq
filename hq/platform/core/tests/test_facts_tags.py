"""Facts: every label and its value a page shows is one part, in a band or a list."""

from django.template import Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, texts, with_class
from hq.platform.core.testing import render


class FactsTagTests(SimpleTestCase):
    def test_a_value_left_out_is_the_missing_mark(self):
        html = render('{% facts %}{% fact "Owner" value=owner %}{% endfacts %}', owner=None)

        (value,) = with_class(html, "fact-value")
        self.assertTrue([mark for mark in with_class(html, "empty-value") if value in mark.ancestors])

    def test_a_detail_given_as_lines_is_a_line_each_with_no_dot(self):
        html = render(
            '{% facts layout="band" %}{% fact "Certificate" value="example" detail=lines %}{% endfacts %}',
            lines=("Example issuer", "", "24 days left"),
        )

        (detail,) = with_class(html, "fact-detail")
        self.assertEqual(texts(html, "small"), ["Example issuer", "24 days left"])
        self.assertTrue(all(detail in small.ancestors for small in elements(html, "small")))
        self.assertNotIn("·", detail.words)

    def test_a_value_that_is_only_a_link_is_the_values_own_link(self):
        """Words around nothing but a link leave it the whole value, drawn as
        a value's link is, never as a link among words."""

        html = render(
            '{% facts %}{% fact "Built from" %}<span class="prose">'
            '<a href="/example/">Name the source</a></span>{% endfact %}{% endfacts %}',
        )

        (link,) = elements(html, "a")
        self.assertIn("fact-shown", link.ancestors[-1].classes)
        self.assertFalse(with_class(html, "prose"))

    def test_a_fact_without_its_label_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% facts %}{% fact value=1 %}{% endfacts %}")
