"""What fast selects: every test that refers to a changed definition, every test that loads a change no name
carries, and every check that reads a changed file."""

import ast
from pathlib import Path

import grimp
from django.test import SimpleTestCase, tag

from hq.domains.control_plane.tests import test_bridge_registry
from hq.platform.application.tests.reads import reads
from hq.platform.core.tests import test_startup
from tools.fonts.tests import test_font
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
        for check in (
            test_startup.StartupBudgetTests,
            test_bridge_registry.WrittenFileTests,
            test_font.ServedFontTests,
        ):
            self.assertFalse(check.tree_reads.selects([LEAF], []), check.__name__)

    def test_a_shipped_change_or_an_imported_helper_selects_the_checks_that_read_it(self):
        for shipped in ("hq/platform/core/apps.py", "uv.lock"):
            self.assertTrue(test_startup.StartupBudgetTests.tree_reads.selects([shipped], []), shipped)
        self.assertTrue(test_font.ServedFontTests.tree_reads.selects(["templates/base.html"], []))
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
@reads("**/*.py", lists=("**",))
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


def named_reach(tree: dict[str, str], edits: dict[str, str]):
    from tools.gate.names import changes, name_reach, outline

    modules = frozenset(selection.module_name(Path(path)) for path in tree)
    every = {
        selection.module_name(Path(p)): outline(ast.parse(edits.get(p, t)), Path(p), modules) for p, t in tree.items()
    }
    seeds = {selection.module_name(Path(p)): changes(tree[p], text) for p, text in edits.items()}
    return name_reach(seeds, every, TESTS | {"example.urls"})


class NameReachTests(SimpleTestCase):
    def test_an_edited_definition_reaches_the_tests_that_refer_to_it_and_no_other_loader(self):
        edited = NAMED["example/limits.py"].replace("LIMIT = 4", "LIMIT = 5")
        found = named_reach(NAMED, {"example/limits.py": edited})

        self.assertEqual(found.names.keys() & TESTS, {"example.tests.test_page", "example.tests.test_patch"})
        self.assertIn("Page", found.names["example.page"])
        self.assertEqual(found.dynamic, set())

    def test_a_comment_or_a_docstring_reaches_nothing(self):
        edited = '"""Limits."""\n# How many.\n' + NAMED["example/limits.py"]
        found = named_reach(NAMED, {"example/limits.py": edited})

        self.assertEqual(found.names, {"example.limits": set()})
        self.assertEqual(found.dynamic, set())

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
