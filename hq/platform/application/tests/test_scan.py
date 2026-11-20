"""A source check's scan reads what its paths name, or refuses."""

from django.test import SimpleTestCase, tag

from .facts import scan


@tag("source", "tree")
class ScanPathTests(SimpleTestCase):
    def test_a_path_that_is_not_there_is_refused_rather_than_read_as_empty(self):
        with self.assertRaisesMessage(FileNotFoundError, "example/moved"):
            scan({"module": {"kind": "module"}}, "example/moved")

    def test_a_rule_inside_the_module_is_refused_and_pointed_at_top(self):
        """ast-grep reports each node a relational rule matched, so each match would carry its whole module."""

        within = {"kind": "import_statement", "inside": {"kind": "module"}}
        deep = {
            "kind": "call",
            "has": {"not": {"inside": {"any": [{"kind": "block"}, {"all": [{"kind": "module"}]}], "stopBy": "end"}}},
        }
        for rule in (within, deep):
            with self.subTest(rule=rule), self.assertRaisesMessage(ValueError, "facts.TOP"):
                scan({"example": rule}, "hq/platform/application/tests/facts.py")

    def test_a_rule_inside_a_block_is_scanned(self):
        found = scan(
            {"example": {"kind": "return_statement", "inside": {"kind": "block", "stopBy": "end"}}},
            "hq/platform/application/tests/facts.py",
        )
        self.assertTrue(found)
