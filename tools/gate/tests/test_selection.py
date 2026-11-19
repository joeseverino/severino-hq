"""What fast selects: every test that loads a changed module."""

import ast

import grimp
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from tools.gate import chain, selection

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
