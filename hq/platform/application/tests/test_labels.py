from django.template import engines
from django.test import SimpleTestCase, tag
from django.utils.safestring import mark_safe

from ..labels import dotted, dotted_html, lower_first, plural
from .markup import elements, links, text


class PluralTests(SimpleTestCase):
    def test_a_noun_is_spelled_as_english_spells_many_of_it(self):
        for one, many in (
            ("host", "hosts"),
            ("calendar entry", "calendar entries"),
            ("Registry", "Registries"),
            ("day", "days"),
            ("address", "addresses"),
            ("match", "matches"),
            ("box", "boxes"),
            ("DNS record", "DNS records"),
        ):
            with self.subTest(one=one):
                self.assertEqual(plural(one), many)

    def test_counted_spells_a_single_noun_the_same_way(self):
        from ..ui import counted

        self.assertEqual(counted(2, "entry"), "2 entries")
        self.assertEqual(counted(1, "entry"), "1 entry")
        self.assertEqual(counted(3, "change"), "3 changes")


class LowerFirstTests(SimpleTestCase):
    def test_a_word_is_lowered_to_read_mid_sentence(self):
        self.assertEqual(lower_first("Proxy host"), "proxy host")
        self.assertEqual(lower_first("A controller must read it"), "a controller must read it")

    def test_an_acronym_keeps_its_case(self):
        for label in ("TLS certificate", "HQ", "DNS record", "IPv6 address"):
            with self.subTest(label=label):
                self.assertEqual(lower_first(label), label)

    def test_nothing_stays_nothing(self):
        self.assertEqual(lower_first(""), "")


class DottedTests(SimpleTestCase):
    def test_each_dot_is_held_to_the_part_before_it(self):
        # A no-break space before the dot: no line starts with one or holds one alone.
        self.assertEqual(dotted("Example Host", "Hosting", 0), "Example Host\xa0· Hosting\xa0· 0")

    def test_a_part_that_is_not_there_leaves_no_dot(self):
        self.assertEqual(dotted(None, "Hosting", "", None), "Hosting")
        self.assertEqual(dotted(), "")

    def test_markup_draws_the_dot_for_the_eye_and_a_reader_hears_the_parts(self):
        html = f'<a href="/x/">{dotted_html("A & B", mark_safe("<em>c</em>"))}</a>'

        self.assertEqual(links(html), [("A & B c", "/x/")])
        self.assertEqual(text(html), "A & B · c")
        self.assertEqual(len(elements(html, "em")), 1)

    def test_the_dot_in_markup_is_the_one_templates_draw(self):
        drawn = engines["django"].from_string("{% load value_tags %}a{% dot %}b").render()

        self.assertEqual(drawn, dotted_html("a", "b"))


@tag("source", "tree")
class LowerFirstSourceTests(SimpleTestCase):
    """One definition of lower_first in the source."""

    def test_there_is_one_definition(self):
        """The rule lived in four places, and only one of them knew about acronyms."""

        from pathlib import Path

        hq = Path(__file__).resolve().parents[3]
        sources = [
            path
            for path in hq.rglob("*.py")
            if "tests" not in path.parts and not path.name.startswith("test") and path.name != "labels.py"
        ]
        self.assertTrue(sources)
        copies = [str(path.relative_to(hq)) for path in sources if "[:1].lower()" in path.read_text()]
        # openapi.py camel-cases an identifier, which is no label.
        self.assertEqual(copies, ["platform/api/openapi.py"])
