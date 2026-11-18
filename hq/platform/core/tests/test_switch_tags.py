"""The switch: a row of links to the same view, one current, is one part."""

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, select, with_class


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


_THREE = (
    '{% switch label="Domains" %}'
    '{% choice url="/one/" current=True mark="★" %}one.example{% endchoice %}'
    '{% choice url="/two/" note="not managed" %}two.example{% endchoice %}'
    '{% choice url="/three/" %}three.example{% endchoice %}'
    "{% endswitch %}"
)


class SwitchTagTests(SimpleTestCase):
    def test_it_is_a_named_navigation_of_links_with_one_current(self):
        html = _render(_THREE)

        (nav,) = elements(html, "nav")
        self.assertEqual(nav.attrs["aria-label"], "Domains")
        links = elements(html, "a")
        self.assertEqual([link.attrs["href"] for link in links], ["/one/", "/two/", "/three/"])
        self.assertTrue(all(nav in link.ancestors for link in links))
        (current,) = select(html, "aria-current")
        self.assertEqual(current, links[0])
        self.assertEqual(current.attrs["aria-current"], "page")

    def test_each_link_is_a_shared_chip(self):
        html = _render(_THREE)

        chips = with_class(html, "scope-chip")
        self.assertEqual([chip.words for chip in chips], ["★ one.example", "two.example not managed", "three.example"])
        for chip, link in zip(chips, elements(html, "a"), strict=True):
            self.assertIn(link, chip.ancestors)

    def test_a_mark_is_hidden_from_a_reader_and_a_note_is_in_the_chip(self):
        html = _render(_THREE)

        (mark,) = select(html, "aria-hidden", "true")
        self.assertEqual(mark.words, "★")
        two = with_class(html, "scope-chip")[1]
        self.assertIn("not managed", two.words)

    def test_a_count_is_in_its_link_beside_its_chip_and_nought_is_drawn(self):
        html = _render(
            '{% switch label="Status" %}'
            '{% choice url="?" current=True count=12 %}All{% endchoice %}'
            '{% choice url="?status=read" count=0 %}Read{% endchoice %}'
            '{% choice url="?status=new" %}New{% endchoice %}'
            "{% endswitch %}"
        )

        links = elements(html, "a")
        counts = with_class(html, "count")
        self.assertEqual([count.words for count in counts], ["12", "0"])
        for count, link in zip(counts, links, strict=False):
            self.assertIn(link, count.ancestors)
            self.assertFalse(any("scope-chip" in ancestor.classes for ancestor in count.ancestors))
        self.assertEqual(links[2].words, "New")

    def test_a_summary_is_said_under_the_row_only_when_given(self):
        html = _render(
            '{% switch label="Tools" %}{% choice url="?tab=a" current=True %}A{% endchoice %}'
            "{% summary %}What A answers.{% endswitch %}"
        )

        (nav,) = elements(html, "nav")
        (summary,) = elements(html, "p")
        self.assertEqual(summary.words, "What A answers.")
        self.assertNotIn(nav, summary.ancestors)
        self.assertEqual(elements(_render(_THREE), "p"), [])

    def test_what_a_page_gives_is_escaped(self):
        html = _render(
            "{% switch label=label %}{% choice url=url note=note %}{{ words }}{% endchoice %}{% endswitch %}",
            label='a"b',
            url="/x/?a=1&b=2",
            note="<i>n</i>",
            words="<b>w</b>",
        )

        self.assertEqual(elements(html, "i"), [])
        self.assertEqual(elements(html, "b"), [])
        (nav,) = elements(html, "nav")
        self.assertEqual(nav.attrs["aria-label"], 'a"b')
        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], "/x/?a=1&b=2")

    def test_a_switch_without_a_label_or_a_choice_without_a_url_is_refused(self):
        with self.assertRaisesMessage(TemplateSyntaxError, "switch is given label="):
            _render('{% switch %}{% choice url="/" %}A{% endchoice %}{% endswitch %}')
        with self.assertRaisesMessage(TemplateSyntaxError, "choice is given url="):
            _render('{% switch label="L" %}{% choice %}A{% endchoice %}{% endswitch %}')
