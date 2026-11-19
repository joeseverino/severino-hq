"""What fast selects: every test that loads a changed module, and every test the map says ran a changed line."""

import ast
import subprocess
import tempfile
from pathlib import Path

import grimp
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.application.tests.test_pipeline import jobs, steps, workflow
from tools.gate import chain, selection, testmap

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
        """The walk along the imports a module runs as it loads goes all the way, as grimp reads them."""

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


def diff(before: str, after: str) -> str:
    """git's diff of no context between two versions of a file."""

    with tempfile.TemporaryDirectory() as folder:
        old, new = Path(folder, "old.py"), Path(folder, "new.py")
        old.write_text(before)
        new.write_text(after)
        # git diff --no-index exits 1 when the files differ.
        done = subprocess.run(
            ["git", "diff", "--no-index", "-U0", "--no-color", str(old), str(new)],
            check=False,
            capture_output=True,
            text=True,
        )
    return done.stdout


BEFORE = '''"""A module."""

LIMIT = 3


def first(value):
    """The first of them."""
    found = value[0]
    return found


class Holder:
    size = 2

    def second(self, value):
        kept = value[1]
        return kept
'''


class MapLineTests(SimpleTestCase):
    """The tests the map says ran a line the change touches are selected; a change outside every function
    body is left to the rules that read imports."""

    def selected(self, after: str, ran: dict[int, frozenset[str]]) -> frozenset[str] | None:
        old, new = testmap.touched(diff(BEFORE, after))
        if testmap.touched_bodies(after, new) is None:
            return None
        lines = testmap.touched_bodies(BEFORE, old)
        if lines is None:
            return None
        return testmap.TestMap(frozenset(), {"a.py": ran}).tests_of("a.py", lines)

    RAN = {
        8: frozenset({"tests.test_first"}),
        9: frozenset({"tests.test_first", "tests.test_both"}),
        16: frozenset({"tests.test_second", "tests.test_both"}),
        17: frozenset({"tests.test_second"}),
    }

    def test_a_changed_line_selects_every_test_that_ran_its_function(self):
        after = BEFORE.replace("    return found", "    return found or None")
        self.assertEqual(self.selected(after, self.RAN), {"tests.test_first", "tests.test_both"})
        after = BEFORE.replace("        kept = value[1]", "        kept = value[-1]")
        self.assertEqual(self.selected(after, self.RAN), {"tests.test_second", "tests.test_both"})

    def test_a_line_added_inside_a_body_selects_the_tests_that_ran_it(self):
        after = BEFORE.replace('    """The first of them."""\n', '    """The first of them."""\n    print(value)\n')
        self.assertEqual(self.selected(after, self.RAN), {"tests.test_first", "tests.test_both"})

    def test_a_change_outside_every_function_body_is_not_read_from_the_map(self):
        for after in (
            BEFORE.replace("LIMIT = 3", "LIMIT = 4"),
            BEFORE.replace("    size = 2", "    size = 3"),
            BEFORE.replace("def first(value):", "def first(value, other=None):"),
            BEFORE.replace('"""The first of them."""', '"""The first one."""'),
            BEFORE + "\n\ndef third():\n    return 3\n",
            # Added between two lines of a body, dedented out of it.
            BEFORE.replace("    found = value[0]\n", "    found = value[0]\nLATER = 1\n"),
        ):
            with self.subTest(after=after):
                self.assertIsNone(self.selected(after, self.RAN))

    def test_a_map_reads_back_as_it_was_written(self):
        held = testmap.TestMap(frozenset({"tests.test_first", "tests.test_second"}), {"a.py": self.RAN})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "map.json")
            testmap.write(path, held)
            self.assertEqual(testmap.load(path), held)


@tag("source")
class MapArtifactTests(SimpleTestCase):
    def test_ci_keeps_the_map_where_fast_fetches_it(self):
        """fast finds CI's map by the artifact's name, and reads from it the file fast:map writes."""

        uploads = {
            step["with"]["name"]: step["with"]["path"]
            for step in steps(jobs(workflow("map.yml"))["map"])
            if step.get("uses", "").startswith("actions/upload-artifact@")
        }
        self.assertEqual(uploads.get(chain.MAP_ARTIFACT), testmap.WRITTEN.as_posix())
