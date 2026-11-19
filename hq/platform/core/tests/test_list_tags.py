"""The list: every list a page writes is one part, read as its kind says."""

from django.template import TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.core.testing import render


class ListTagTests(SimpleTestCase):
    def test_a_list_with_no_item_draws_nothing(self):
        self.assertEqual(
            render(
                '{% list kind="dots" %}{% for name in names %}<li>{{ name }}</li>{% endfor %}{% endlist %}', names=[]
            ),
            "",
        )

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
