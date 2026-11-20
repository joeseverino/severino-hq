"""What fast selects: every test that refers to a changed definition, every test that loads a change no name
carries, and every check that reads a changed file."""

import ast
import tempfile
from pathlib import Path

import grimp
from django.test import SimpleTestCase, tag

from tools.gate import chain, selection

# An edit inside one test's body, which no other test reads: a module no source names, so none is chosen by it.
LEAF = "hq/platform/mcp/tests/test_example_leaf.py"
PROBES = Path("hq/platform/core/tests/test_startup.py")


@tag("source")
class ReaderSelectionTests(SimpleTestCase):
    def test_a_changed_test_runs_and_no_check_is_selected_by_its_name(self):
        changed = [Path(LEAF), Path("hq/platform/application/tests/test_architecture_pages.py")]
        files = [*changed, Path("hq/platform/application/tests/test_architecture.py"), PROBES]
        modules = sorted(map(selection.module_name, changed))

        self.assertEqual(selection.test_labels(files, set(modules), set())[0], modules)

    def test_an_imported_helper_selects_the_checks_that_read_it(self):
        helper = Path("hq/platform/application/tests/facts.py")
        reached = selection.import_reach({selection.module_name(helper)}, selection.importers([helper, PROBES]))
        self.assertIn(selection.module_name(PROBES), reached)


# Modules across the apps and layers, each imported by tests of other apps as they load.
SAMPLE = (
    "hq.domains.control_plane.dns_lookup",
    "hq.domains.contacts.inbox",
    "hq.domains.receipts.validation",
    "hq.domains.expenses.models",
    "hq.platform.application.capabilities",
    "hq.platform.core.rules",
    "hq_sdk.ui",
)


@tag("source", "tree")
class LoadedImportTests(SimpleTestCase):
    def test_a_change_selects_every_test_module_that_loads_it(self):
        """The walk along the imports a module runs as it loads, which a change no name carries takes, goes all
        the way, as grimp reads them."""

        files = selection.python_files()
        tests = {selection.module_name(path) for path in files if selection.is_test(path)}
        paths = {selection.module_name(path): path for path in files}
        graph = grimp.build_graph("hq", "hq_sdk", "tests", "tools", cache_dir=None)
        for importer in graph.modules & paths.keys():
            lines = selection.load_lines(ast.parse((chain.ROOT / paths[importer]).read_text()))
            for imported in graph.find_modules_directly_imported_by(importer):
                details = graph.get_import_details(importer=importer, imported=imported)
                if not any(not d["is_lazy"] and d["line_number"] in lines for d in details):
                    graph.remove_import(importer=importer, imported=imported)
        for module in SAMPLE:
            with self.subTest(module=module):
                expected = graph.find_downstream_modules(module) & tests
                self.assertGreater(len(expected), 1, "the sample module is loaded by too few tests to tell")
                self.assertLessEqual(expected, selection.importing({module}, {}, files))


# A tree of its own: a constant two definitions read, one of them through a page a test drives, the other
# through a registry another test reads, and a test that patches one by its dotted name.
NAMED = {
    "example/limits.py": "LIMIT = 4\nOTHER = 1\n\n\ndef recent(rows):\n    return rows[:LIMIT]\n\n\ndef unread():\n    return OTHER\n",
    "example/page.py": "from example import limits\n\n\nclass Page:\n    def get(self):\n        return limits.recent([])\n",
    "example/registry.py": "from example.limits import unread\n\nCOUNTS = (unread,)\n",
    "example/urls.py": "from example.page import Page\n\nurlpatterns = [Page]\n",
    "example/tests/test_page.py": "from example.page import Page\n\n\nclass PageTests:\n    page = Page\n",
    "example/tests/test_registry.py": "from example import registry\n\n\nclass CountTests:\n    counts = registry.COUNTS\n",
    "example/tests/test_patch.py": 'class PatchTests:\n    target = "example.limits.recent"\n',
}
TESTS = {"example.tests.test_page", "example.tests.test_registry", "example.tests.test_patch"}


def named_reach(tree: dict[str, str], edits: dict[str, str], recorded: dict[str, frozenset[str]] | None = None):
    from tools.gate.names import changes, name_reach, outline

    modules = frozenset(selection.module_name(Path(path)) for path in tree)
    every = {
        selection.module_name(Path(p)): outline(ast.parse(edits.get(p, t)), Path(p), modules) for p, t in tree.items()
    }
    seeds = {selection.module_name(Path(p)): changes(tree[p], text) for p, text in edits.items()}
    return name_reach(seeds, every, TESTS | {"example.urls"}, recorded)


# Every module of the tree but the tests, as a record of what each test ran holds them, with no body run as
# the suite started.
RECORDED = dict.fromkeys(("example.limits", "example.page", "example.registry", "example.urls"), frozenset[str]())


class NameReachTests(SimpleTestCase):
    def test_an_edited_definition_reaches_the_tests_that_refer_to_it_and_no_other_loader(self):
        edited = NAMED["example/limits.py"].replace("LIMIT = 4", "LIMIT = 5")
        found = named_reach(NAMED, {"example/limits.py": edited})

        self.assertEqual(found.names.keys() & TESTS, {"example.tests.test_page", "example.tests.test_patch"})
        self.assertIn("Page", found.names["example.page"])
        self.assertEqual(found.dynamic, set())

    def test_a_comment_or_a_docstring_reaches_nothing(self):
        edited = '"""Limits."""\n# How many.\n' + NAMED["example/limits.py"]
        found = named_reach(NAMED, {"example/limits.py": edited}, RECORDED)

        self.assertEqual((found.names, found.ran, found.dynamic), ({}, {}, set()))

    def test_what_a_name_cannot_carry_reaches_every_module_that_loads_it(self):
        """A bare statement, a definition a decorator of this tree takes or one nothing refers to."""

        base = NAMED["example/limits.py"]
        for edit in (
            base + "\nprint(LIMIT)\n",
            base.replace("def recent", "@unread\ndef recent"),
            base + "\n\ndef unused():\n    return LIMIT\n",
        ):
            with self.subTest(edit=edit.splitlines()[-1]):
                found = named_reach(NAMED, {"example/limits.py": edit.replace("LIMIT = 4", "LIMIT = 5")})
                self.assertEqual(found.dynamic, {"example.limits"})


class RecordedReachTests(SimpleTestCase):
    """A body the record holds reaches the tests that ran it, and through no definition that refers to it."""

    def test_an_edited_body_reaches_the_tests_that_ran_it_and_none_that_refer_to_it(self):
        edited = NAMED["example/limits.py"].replace("return rows[:LIMIT]", "return rows[LIMIT:]")
        found = named_reach(NAMED, {"example/limits.py": edited}, RECORDED)

        self.assertEqual((found.names, found.ran), ({}, {"example.limits": {"recent"}}))
        # Read from the source alone, it reaches every test that refers to the page.
        self.assertIn("example.tests.test_page", named_reach(NAMED, {"example/limits.py": edited}).names)

    def test_a_constant_reaches_the_bodies_that_read_it(self):
        edited = NAMED["example/limits.py"].replace("LIMIT = 4", "LIMIT = 5")
        found = named_reach(NAMED, {"example/limits.py": edited}, RECORDED)

        self.assertEqual(found.ran, {"example.limits": {"recent"}})
        self.assertNotIn("example.tests.test_page", found.names)

    def test_a_body_a_module_runs_as_it_loads_or_a_cache_keeps_reaches_what_reads_its_answer(self):
        tree = NAMED | {"example/registry.py": "from example.limits import unread\n\nCOUNTS = (unread(),)\n"}
        cached = NAMED["example/limits.py"].replace("def unread", "@functools.cache\ndef unread")
        for name, base in (("called as it loads", tree), ("cached", NAMED | {"example/limits.py": cached})):
            with self.subTest(name):
                edited = base["example/limits.py"].replace("return OTHER", "return OTHER + 1")
                found = named_reach(base, {"example/limits.py": edited}, RECORDED)
                self.assertIn("example.tests.test_registry", found.names)

    def test_a_body_that_ran_as_the_suite_started_reaches_what_refers_to_it(self):
        edited = NAMED["example/limits.py"].replace("return OTHER", "return OTHER + 1")
        found = named_reach(NAMED, {"example/limits.py": edited}, RECORDED | {"example.limits": frozenset({"unread"})})

        self.assertIn("example.tests.test_registry", found.names)

    def test_a_body_a_registry_only_holds_reaches_no_reader_of_the_registry(self):
        edited = NAMED["example/limits.py"].replace("return OTHER", "return OTHER + 1")
        found = named_reach(NAMED, {"example/limits.py": edited}, RECORDED)

        self.assertEqual((found.names, found.ran), ({}, {"example.limits": {"unread"}}))

    def test_a_type_a_signature_names_reaches_only_a_function_a_decorator_of_this_tree_takes(self):
        limits = NAMED["example/limits.py"] + "\n\nclass Shape:\n    size = 1\n"
        page = "from example.limits import Shape, unread\n\n\n{}def show(shape: Shape) -> int:\n    return 1\n"
        edited = limits.replace("size = 1", "size = 2")
        for decorator, reached in (("", False), ("@unread\n", True)):
            with self.subTest(decorator=decorator):
                tree = NAMED | {"example/limits.py": limits, "example/page.py": page.format(decorator)}
                found = named_reach(tree, {"example/limits.py": edited}, RECORDED)
                self.assertEqual("show" in found.names.get("example.page", set()), reached)

    def test_an_added_method_reaches_the_tests_that_ran_its_class(self):
        edited = NAMED["example/page.py"] + "\n    def post(self):\n        return 1\n"
        found = named_reach(NAMED, {"example/page.py": edited}, RECORDED)

        self.assertIn("Page.get", found.ran["example.page"])
        self.assertEqual(found.dynamic, set())

    def test_a_decorator_imported_from_outside_the_tree_leaves_a_class_to_its_names(self):
        from tools.gate.names import outline

        text = "from dataclasses import dataclass\n\n\n@dataclass\nclass Row:\n    size: int = 1\n"
        statements = outline(ast.parse(text), Path("example/rows.py"), frozenset({"example.rows"}))

        self.assertFalse(statements[-1].decorated)

    def test_the_record_answers_with_the_tests_and_the_start_that_ran_a_line_of_a_body(self):
        from coverage import CoverageData

        from tools.gate.ran import Record

        with tempfile.TemporaryDirectory() as folder:
            data = CoverageData(str(Path(folder, "data")))
            ran = (("", [10]), ("example.tests.test_page", [6]), ("example.tests.test_registry", [10]))
            for test, lines in ran:
                data.set_context(test)
                data.add_lines({"example/limits.py": lines})
            data.write()
            record = Record("tree", Path(folder, "data"))
            texts = {"example/limits.py": NAMED["example/limits.py"]}

            self.assertEqual(record.tests, {"example.tests.test_page", "example.tests.test_registry"})
            self.assertEqual(record.ran({"example.limits": {"recent"}}, texts), {"example.tests.test_page"})
            self.assertEqual(record.started(texts)["example.limits"], {"unread"})
