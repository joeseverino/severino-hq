"""Facts: every label and its value a page shows is one part, in a band or a list."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, select, texts, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class FactsTagTests(SimpleTestCase):
    def test_a_fact_is_its_label_its_value_and_its_detail(self):
        html = _render('{% facts %}{% fact "Address" value=address detail="Read today" %}{% endfacts %}', address="a.b")

        (listed,) = elements(html, "dl")
        (fact,) = with_class(html, "fact")
        self.assertIn(listed, fact.ancestors)
        self.assertEqual(texts(html, "dt"), ["Address"])
        self.assertEqual([value.words for value in with_class(html, "fact-value")], ["a.b"])
        self.assertEqual([detail.words for detail in with_class(html, "fact-detail")], ["Read today"])

    def test_a_zero_and_a_linked_count_are_one_element_alike(self):
        # The defect this part closes: "0 rules" drawn as a span beside
        # "13 refused" drawn as a link, and the two stood apart.
        html = _render(
            '{% facts layout="band" %}{% fact "Rules" value=0 %}'
            '{% fact "Refused" value=13 url="/example/?refused=1" %}{% endfacts %}'
        )

        values = with_class(html, "fact-value")
        self.assertEqual([value.words for value in values], ["0", "13"])
        self.assertEqual({value.tag for value in values}, {"dd"})
        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], "/example/?refused=1")
        self.assertIn(values[1], link.ancestors)
        self.assertEqual(link.words, "13")

    def test_a_value_left_out_is_the_missing_mark(self):
        html = _render('{% facts %}{% fact "Owner" value=owner %}{% endfacts %}', owner=None)

        (value,) = with_class(html, "fact-value")
        self.assertTrue([mark for mark in with_class(html, "empty-value") if value in mark.ancestors])

    def test_a_block_fact_holds_markup_and_its_detail(self):
        html = _render(
            '{% facts layout="band" %}{% fact "Version" icon="layers" tone=tone %}<code>{{ tag }}</code>'
            "{% detail %}<small>Tag only</small><small>Built today</small>{% endfact %}{% endfacts %}",
            tag="1.2",
            tone="serious",
        )

        (fact,) = with_class(html, "fact")
        self.assertIn("tone-serious", fact.classes)
        self.assertEqual(len(with_class(html, "band-icon")), 1)
        (value,) = with_class(html, "fact-value")
        self.assertEqual(value.words, "1.2")
        self.assertEqual(texts(html, "small"), ["Tag only", "Built today"])
        (detail,) = with_class(html, "fact-detail")
        self.assertTrue(all(detail in small.ancestors for small in elements(html, "small")))

    def test_without_a_tone_an_icon_or_a_detail_it_has_none(self):
        html = _render('{% facts %}{% fact "Running" value=3 tone=tone %}{% endfacts %}', tone="")

        (fact,) = with_class(html, "fact")
        self.assertEqual(fact.classes, {"fact"})
        self.assertEqual(with_class(html, "band-icon"), [])
        self.assertEqual(with_class(html, "fact-detail"), [])

    def test_each_layout_is_its_own_look_and_a_band_may_be_named(self):
        band = _render('{% facts layout="band" label="Example status" %}{% fact "A" value=1 %}{% endfacts %}')
        listed = _render('{% facts %}{% fact "A" value=1 %}{% endfacts %}')

        (named,) = select(band, "aria-label", "Example status")
        self.assertIn("facts-band", named.classes)
        (plain,) = elements(listed, "dl")
        self.assertIn("facts-list", plain.classes)
        self.assertNotIn("aria-label", plain.attrs)

    def test_a_detail_given_as_lines_is_a_line_each_with_no_dot(self):
        html = _render(
            '{% facts layout="band" %}{% fact "Certificate" value="example" detail=lines %}{% endfacts %}',
            lines=("Example issuer", "", "24 days left"),
        )

        (detail,) = with_class(html, "fact-detail")
        self.assertEqual(texts(html, "small"), ["Example issuer", "24 days left"])
        self.assertTrue(all(detail in small.ancestors for small in elements(html, "small")))
        self.assertNotIn("·", detail.words)

    def test_a_fact_without_its_label_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% facts %}{% fact value=1 %}{% endfacts %}")
