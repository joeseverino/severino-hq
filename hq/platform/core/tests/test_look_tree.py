"""A look names a change made while it draws only when a page or the look
itself reads the file: a test, a lint or type-checker setting or a tool
neither runs leaves its tree as it began."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, tag

from hq.platform.core.tests import look

# A checkout in miniature: a page's template and view, the look with a helper
# and a tool it imports and a shell file its task sources, beside a test, a
# lint rule, a task file and pyproject.toml's settings, which neither reads.
FILES = {
    "templates/page.html": "<main>Example</main>\n",
    "hq/__init__.py": "",
    "hq/platform/__init__.py": "",
    "hq/platform/core/__init__.py": "",
    "hq/platform/core/views.py": "PAGE = 'page.html'\n",
    "hq/platform/core/tests/__init__.py": "",
    "hq/platform/core/tests/look.py": (
        "from hq.platform.core.tests import look_helper\nfrom tools.perf import report\n"
    ),
    "hq/platform/core/tests/look_helper.py": "WIDTH = 820\n",
    "hq/platform/core/tests/test_example.py": "def test_example():\n    pass\n",
    "tools/__init__.py": "",
    "tools/perf/__init__.py": "",
    "tools/perf/report.py": "LINE = 'perf'\n",
    "tools/lint/rule.yml": "id: example\n",
    "tools/lib/look.sh": "LOOKED=1\n",
    "mise-tasks/dev/look": "#!/bin/sh\n. ./tools/lib/look.sh\n",
    "mise-tasks/lint.toml": "[tasks.example]\nrun = 'true'\n",
    "pyproject.toml": "[tool.mypy]\nstrict = true\n",
}


@tag("source")
class TreeChangedTests(SimpleTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name, "checkout")
        for path, text in FILES.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.store = Path(folder.name, "store")
        self.tree = look.tree_of(self.root, self.store)

    def changed(self, *paths):
        for path in paths:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            with (self.root / path).open("a", encoding="utf-8") as file:
                file.write("# changed\n")
        return look.changed_since(self.tree, self.root, self.store)

    def test_a_test_a_lint_setting_or_a_tool_no_look_runs_keeps_the_tree(self):
        moved = self.changed(
            "hq/platform/core/tests/test_example.py",
            "hq/platform/core/tests/test_new.py",
            "pyproject.toml",
            "tools/lint/rule.yml",
            "mise-tasks/lint.toml",
        )

        self.assertEqual(moved, [])

    def test_a_template_change_is_named(self):
        self.assertEqual(self.changed("templates/page.html"), ["templates/page.html"])

    def test_a_change_to_the_looks_own_code_is_named(self):
        own = ("hq/platform/core/tests/look_helper.py", "tools/perf/report.py", "tools/lib/look.sh")

        self.assertCountEqual(self.changed(*own), own)
