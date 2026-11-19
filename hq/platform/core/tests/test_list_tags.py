"""The list: every list a page writes is one part, read as its kind says."""

from django.template import TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, select
from hq.platform.core.testing import render


class ListTagTests(SimpleTestCase):
    def test_its_items_are_the_pages_with_the_pages_hooks_in_every_kind(self):
        from hq.platform.core.templatetags.list_tags import KINDS

        for kind in KINDS:
            with self.subTest(kind=kind):
                html = render(
                    f'{{% list kind="{kind}" data_example="example" %}}<li>An example item</li><li>{{{{ note }}}}</li>'
                    "{% endlist %}",
                    note="<b>escaped</b>",
                )

                (listed,) = select(html, "data-example", "example")
                self.assertEqual([item.words for item in elements(html, "li")], ["An example item", "<b>escaped</b>"])
                self.assertTrue(all(listed in item.ancestors for item in elements(html, "li")))

    def test_a_list_with_no_item_draws_nothing(self):
        self.assertEqual(
            render(
                '{% list kind="dots" %}{% for name in names %}<li>{{ name }}</li>{% endfor %}{% endlist %}', names=[]
            ),
            "",
        )

    def test_each_kind_s_own_option_draws(self):
        below = render('{% list below="Affected" %}<li>example.com</li>{% endlist %}')
        (waiting,) = select(below, "data-label", "Affected")
        self.assertIn("row-below", waiting.classes)

        (quiet,) = elements(render('{% list kind="dots" muted=True %}<li>example.com</li>{% endlist %}'), "ul")
        self.assertIn("muted", quiet.classes)

        numbered = render('{% list kind="prose" numbered=True %}<li>First.</li><li>Second.</li>{% endlist %}')
        self.assertEqual(len(elements(numbered, "ol")), 1)

    def test_an_option_of_another_kind_is_refused(self):
        for written in ('kind="dots" below="Affected"', "muted=True", 'kind="chips" numbered=True'):
            with self.subTest(written=written), self.assertRaisesMessage(TemplateSyntaxError, "is for kind="):
                render(f"{{% list {written} %}}<li>example.com</li>{{% endlist %}}")

    def test_words_outside_an_item_are_refused(self):
        """Words between the items would join a run with no dot before them,
        or stand outside every line."""

        for items in ("Online<li>example-host</li>", "<li>Online</li> since today"):
            with self.subTest(items=items), self.assertRaises(TemplateSyntaxError):
                render(f'{{% list kind="dots" %}}{items}{{% endlist %}}')

    def test_an_extension_may_write_it(self):
        from hq.platform.core.template_contract import describe

        self.assertIn("list", describe()["tags"])
