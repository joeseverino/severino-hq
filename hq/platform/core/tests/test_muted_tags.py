from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import select, with_class


def drawn(source: str, **context) -> str:
    return Template("{% load value_tags %}" + source).render(Context(context))


class MutedTests(SimpleTestCase):
    """Words in the muted voice, drawn by the one tag that writes them."""

    def test_the_words_are_drawn_in_the_muted_voice_as_a_line_or_a_paragraph(self):
        line = drawn("<p>3 checks {% muted %}since {{ when }}{% endmuted %}</p>", when="Monday")
        paragraph = drawn('{% muted element="p" %}A sentence under it.{% endmuted %}')

        self.assertEqual([part.tag for part in with_class(line, "muted")], ["span"])
        self.assertEqual(with_class(line, "muted")[0].words, "since Monday")
        self.assertEqual([part.tag for part in with_class(paragraph, "muted")], ["p"])

    def test_where_it_stands_adds_its_classes_and_attributes(self):
        shown = drawn(
            '{% muted css="row-more" title="Example" data_tip="Why" %}Example{% endmuted %}',
        )

        (part,) = with_class(shown, "muted")
        self.assertIn("row-more", part.classes)
        self.assertEqual(part.attrs.get("title"), "Example")
        self.assertEqual([found.attrs["data-tip"] for found in select(shown, "data-tip")], ["Why"])

    def test_a_tip_is_reached_by_a_press_and_a_scripted_line_waits_hidden(self):
        tipped = drawn("{% muted tip=note %}pinned{% endmuted %}", note="sha256:example")
        waiting = drawn('{% muted hidden=True role="status" data_submit_status=True %}Working{% endmuted %}')

        (tip,) = with_class(tipped, "muted")
        self.assertEqual((tip.attrs.get("data-tip"), tip.attrs.get("tabindex")), ("sha256:example", "0"))
        (line,) = with_class(waiting, "muted")
        self.assertIn("hidden", line.attrs)
        self.assertEqual(line.attrs.get("role"), "status")
        self.assertEqual(len(select(waiting, "data-submit-status")), 1)

    def test_a_block_of_words_is_muted_whole(self):
        block = drawn('{% muted element="div" %}A description.{% endmuted %}')

        self.assertEqual([part.tag for part in with_class(block, "muted")], ["div"])

    def test_an_extension_may_write_it(self):
        from hq.platform.core.template_contract import describe

        self.assertIn("muted", describe()["tags"])
