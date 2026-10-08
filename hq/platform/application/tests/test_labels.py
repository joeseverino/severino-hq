from django.test import SimpleTestCase

from ..labels import lower_first, plural


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

    def test_there_is_one_definition(self):
        """The rule lived in four places, and only one of them knew about acronyms."""

        from pathlib import Path

        root = Path(__file__).resolve().parents[4]
        copies = [
            str(path.relative_to(root))
            for folder in ("application", "control_plane", "core")
            for path in (root / folder).rglob("*.py")
            if not path.name.startswith("test") and "[:1].lower() + " in path.read_text() and path.name != "labels.py"
        ]
        self.assertEqual(copies, [])
