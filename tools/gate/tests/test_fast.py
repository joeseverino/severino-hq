"""The inner-loop gate maps a change to the tests and quick gates that reach
it, runs them in one mise run, and names every failed step in its verdict."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import tag
from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from hq.config.sandbox import RUN
from hq.platform.application.tests.tree import SUITE
from hq.platform.core.test_runner import excluded_tags
from hq.platform.core.tests.task import Usage, names, trusted_mise
from tests.mise_tasks import mise_tasks
from tools.gate import chain as fast_chain, fast as fast_gate, selection as fast_select, verdict as gate_verdict
from tools.gate.tests.base import UNITTEST, GateTestCase, fixture_git
from tools.gate.tests.fast_runs import Recorder, everywhere, run_main
from tools.gate.verdict import FailedTest

ROOT = Path(__file__).resolve().parents[3]


@tag("source", "tree")
class FastGateMappingTests(GateTestCase):
    def setUp(self):
        self.files = fast_select.python_files()

    def labels(self, *changed: str) -> list[str]:
        reached, touched = fast_select.affected(list(changed), [], self.files)
        return fast_select.test_labels(self.files, reached, touched)[0]

    def test_a_package_init_is_the_package(self):
        self.assertEqual(fast_select.module_name(Path("a/b/__init__.py")), "a.b")
        self.assertEqual(fast_select.module_name(Path("a/b/c.py")), "a.b.c")

    def test_a_relative_import_reaches_its_sibling(self):
        importers = fast_select.importers(self.files)
        self.assertIn("hq.platform.application.tests.test_domains", importers["hq.platform.application.domains"])

    def test_a_declarations_string_reference_is_not_an_import(self):
        importers = fast_select.importers(self.files)
        self.assertNotIn("hq.platform.application.domains", importers["hq.domains.expenses.urls"])

    def test_a_lazy_import_and_a_tests_mock_target_are_imports_and_reach_the_package(self):
        modules = {
            "example/__init__.py": "",
            "example/models.py": "class Thing: ...\n",
            "example/urls.py": "patterns = []\n",
            "example/lazy.py": "lazy from example.models import Thing\n",
            "example/declares.py": 'URLCONF = "example.urls.patterns"\n',
            "example/tests/__init__.py": "",
            "example/tests/test_mock.py": '@patch("example.urls.patterns")\ndef test(): ...\n',
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in modules.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            with patch.object(fast_chain, "ROOT", root):
                importers = fast_select.importers([Path(path) for path in modules])
        self.assertEqual(importers["example.models"], {"example.lazy"})
        self.assertEqual(importers["example.urls"], {"example.tests.test_mock"})
        self.assertLessEqual({"example.lazy", "example.tests.test_mock"}, importers["example"])

    def test_an_app_change_selects_the_tests_that_use_the_app(self):
        labels = self.labels("hq/domains/expenses/views.py")
        self.assertIn("hq.platform.application.tests.test_domains", labels)
        self.assertNotIn("hq.platform.mcp.tests", labels)

    def test_a_model_change_selects_its_app_and_not_an_unrelated_domain(self):
        labels = self.labels("hq/domains/projects/models.py")
        own = [
            fast_select.module_name(p)
            for p in self.files
            if fast_select.is_test(p) and p.parts[:3] == ("hq", "domains", "projects")
        ]
        self.assertTrue(own)
        self.assertLessEqual(set(own), set(labels))
        self.assertFalse([label for label in labels if label.startswith("hq.domains.receipts.")])

    def test_an_app_label_in_prose_is_not_a_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in {
                "namespace": 'reverse("projects:list")',
                "model": 'apps.get_model("projects", "Project")',
                "prose": '"""Covers the projects page and its core."""',
            }.items():
                (root / f"test_{name}.py").write_text(text)
            files = sorted(Path(p.name) for p in root.iterdir())
            with patch.object(fast_chain, "ROOT", root):
                chosen, _ = fast_select.test_labels(files, set(), {"projects"})
            self.assertEqual(chosen, ["test_model", "test_namespace"])

    def reading(self, modules: dict[str, str], *changed: str) -> set[str]:
        """The modules of a fixture tree holding ``modules`` that ``changed`` reaches through what they read."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in {"example/__init__.py": "", "example/tests/__init__.py": "", **modules}.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            with patch.object(fast_chain, "ROOT", root):
                return fast_select.reading(list(changed), sorted(p.relative_to(root) for p in root.rglob("*.py")))

    def test_a_changed_file_selects_the_tests_that_name_its_path_or_directory(self):
        tests = "example/tests/test_{}.py"
        found = self.reading(
            {
                tests.format("string"): 'read(".github/actions/example/action.yml")',
                tests.format("segments"): 'root / ".github" / "actions" / "example" / "action.yml"',
                tests.format("arguments"): 'Path(".github", "actions", "example")',
                tests.format("joined"): 'ACTIONS = root / ".github" / "actions"\nACTIONS / name / "action.yml"',
                tests.format("longer"): 'read(".github/actions/example-other/action.yml")',
                tests.format("other"): 'read(".github/actions/elsewhere/action.yml")',
                tests.format("sibling"): 'read(".github/actions/example/other.yml")',
                tests.format("named_further"): 'root / ".github" / "actions" / "other"',
            },
            ".github/actions/example/action.yml",
        )
        tested = {name.rpartition(".test_")[2] for name in found}
        self.assertEqual(tested, {"string", "segments", "arguments", "joined"})

    def test_a_file_is_named_from_beside_a_module_a_top_level_directory_or_where_django_loads_it(self):
        found = self.reading(
            {
                "example/tests/test_beside.py": 'REPORT = Path(__file__).parent / "report.jq"',
                "example/test_above.py": 'Path(__file__).parent / "tests" / "report.jq"',
                "other/tests/test_elsewhere.py": 'Path(__file__).parent / "report.jq"',
                "example/tests/test_templates.py": 'TEMPLATES = BASE_DIR / "templates"',
                "example/routes.py": 'Mount("templates/", "example.urls")',
                "example/tests/test_render.py": 'render_to_string("partials/example.html")',
                "example/tests/test_static.py": 'static("css/example.css")',
            },
            "example/tests/report.jq",
            "templates/partials/example.html",
            "example/static/css/example.css",
        )
        self.assertEqual(
            found,
            {
                "example.tests.test_beside",
                "example.test_above",
                "example.tests.test_templates",
                "example.tests.test_render",
                "example.tests.test_static",
            },
        )

    def test_a_test_reaches_a_file_through_the_helper_that_names_it_and_no_further(self):
        found = self.reading(
            {
                "example/tests/pipeline.py": 'WORKFLOWS = ROOT / ".github" / "workflows"\n'
                "def workflow(name):\n    return load(WORKFLOWS / name)\n",
                "example/tests/test_scanner.py": 'from example.tests.pipeline import workflow\nworkflow("compose.yml")\n',
                "example/provider.py": 'COMPOSE = ".github/workflows/compose.yml"\n',
                "example/adapter.py": "from example.provider import COMPOSE\n",
                "example/tests/test_adapter.py": "from example.adapter import COMPOSE\n",
            },
            ".github/workflows/compose.yml",
        )
        self.assertLessEqual({"example.tests.test_scanner", "example.adapter"}, found)
        self.assertNotIn("example.tests.test_adapter", found)

    def test_a_module_is_named_whole_and_a_tests_directory_above_it_selects_that_test_alone(self):
        found = self.reading(
            {
                "example/tests/test_whole.py": 'read("example/config/settings.py")',
                "example/tests/test_folder.py": 'root = BASE_DIR / "example/config"\n(root / "settings.py").read_text()',
                "example/tests/test_through.py": "from example.tests.test_folder import root\n",
                "example/tests/test_package.py": 'ROOT / "example"',
            },
            "example/config/settings.py",
        )
        self.assertLessEqual({"example.tests.test_whole", "example.tests.test_folder"}, found)
        self.assertFalse({"example.tests.test_through", "example.tests.test_package"} & found)

    def test_a_change_of_each_kind_selects_the_test_that_reads_it(self):
        """The change that showed the class, a workflow read through a helper, and one of each other kind."""
        for changed, reader in {
            ".github/workflows/compose.yml": "hq.platform.application.tests.test_scanners",
            "mise-tasks/image/verify/tree": "hq.platform.application.tests.test_image_tree",
            "templates/partials/_reading_age.html": "hq.platform.application.tests.test_reading_age",
            "static/css/app/tokens.css": "hq.platform.core.tests.test_browser_gate_tasks",
            "static/js/tables.js": "hq.platform.core.tests.test_table_contract",
            "docs/Deployment.md": "hq.platform.core.tests.test_settings_env",
            "tools/pipeline/codeql_report.jq": "tools.pipeline.tests.test_codeql_report",
            "hq/config/settings.py": "hq.platform.application.tests.test_domains",
        }.items():
            with self.subTest(changed=changed):
                self.assertIn(reader, self.labels(changed))

    def test_the_paths_a_test_builds_are_those_it_reads_and_not_those_it_walks_or_compares(self):
        from hq.platform.application.tests.tree import named_paths

        helper = (
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parents[3]\n"
            'WORKFLOWS = ROOT / ".github" / "workflows"\n'
            "def workflow(name):\n    return load(WORKFLOWS / name)\n"
            "def load(path):\n    return path.read_text()\n"
        )
        reads = (
            "from pathlib import Path\n"
            "from tools.example.tests.pipeline import ROOT, workflow\n"
            'REPORT = Path(__file__).parent / "report.jq"\n'
            "def test():\n"
            '    workflow("compose.yml")\n'
            '    docs = ROOT / "docs"\n'
            '    (docs / "Guide.md").read_text()\n'
            '    for path in (ROOT / "templates").rglob("*.html"):\n'
            '        path.relative_to(ROOT / "static")\n'
            "    return f\"{ROOT / 'other'}\", REPORT.read_text()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in {
                "tools/__init__.py": "",
                "tools/example/__init__.py": "",
                "tools/example/tests/__init__.py": "",
                "tools/example/tests/pipeline.py": helper,
                "tools/example/tests/test_reads.py": reads,
                "tools/example/tests/report.jq": "",
                ".github/workflows/compose.yml": "",
                "docs/Guide.md": "",
                "templates/page.html": "",
                "static/app.css": "",
                "other/file": "",
            }.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            built = named_paths(root)["tools.example.tests.test_reads"]
        self.assertEqual(built, [".github/workflows/compose.yml", "docs/Guide.md", "tools/example/tests/report.jq"])

    def test_a_change_to_any_file_a_test_builds_selects_that_test(self):
        """Every path a test module builds, read by tree.Named: a file, or for a directory a file under it
        that is not Python, since a package is the tree tag's. A miss is a way of naming a file that
        fast.reading does not read."""
        from hq.platform.application.tests.tree import named_paths

        tracked = names("ls-files", root=ROOT)
        built = named_paths(ROOT)
        self.assertIn(".github/workflows/compose.yml", built["hq.platform.application.tests.test_scanners"])
        selected: dict[str, tuple[set[str], set[str]]] = {}
        missed = []
        for module, paths in built.items():
            for path in paths:
                under = (f for f in tracked if f.startswith(f"{path}/") and not f.endswith(".py"))
                sample = path if (ROOT / path).is_file() else next(under, None)
                if sample and sample not in selected:
                    selected[sample] = fast_select.affected([sample], [], self.files)
                # A test the change reaches is selected; one that is not may yet be, by an app it names.
                if sample and module not in selected[sample][0] and module not in self.labels(sample):
                    missed.append(f"{module} builds {path}")
        self.assertEqual(missed, [], "fast does not select these tests for a change to a file they read")

    def test_a_task_definition_change_selects_the_tests_that_read_the_tasks(self):
        """A task moved between mise.toml and mise-tasks/ is read through tests.mise_tasks, by no path."""
        for changed in ("mise.toml", "mise-tasks/browser.toml", "mise-tasks/test/composed"):
            with self.subTest(changed=changed):
                self.assertIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels(changed))
        self.assertNotIn("hq.platform.core.tests.test_browser_gate_tasks", self.labels("docs/Development.md"))

    def test_a_change_to_a_file_a_task_runs_selects_the_tests_that_read_the_tasks(self):
        """GateReferenceTests reads the scripts and modules a task's run names, by no import of them."""
        reader = "hq.platform.application.tests.test_pipeline"
        for changed in ("tools/dev/dev.sh", "tools/gate/fast.py", "tools/structural/bar.py"):
            with self.subTest(changed=changed):
                self.assertIn(reader, self.labels(changed))
        self.assertNotIn(reader, self.labels("tools/gate/turn.py"))

    def test_a_test_change_selects_that_test_and_the_architecture_tests(self):
        labels = self.labels("hq/platform/core/tests/test_security.py")
        self.assertIn("hq.platform.core.tests.test_security", labels)
        self.assertIn("hq.platform.application.tests.test_architecture", labels)

    def test_a_template_belongs_to_its_app(self):
        _, touched = fast_select.affected(["templates/expenses/list.html"], [], self.files)
        self.assertEqual(touched, {"expenses"})

    def test_a_deleted_module_selects_what_imported_it(self):
        reached, touched = fast_select.affected([], ["hq/domains/projects/models.py"], self.files)
        self.assertEqual(touched, {"projects"})
        self.assertIn("hq.domains.reports.exports", reached)

    def test_apps_are_derived_from_the_tree(self):
        self.assertLessEqual({"expenses", "core", "projects"}, set(fast_select.django_apps().values()))


@tag("source")
class FastGateTreeTests(GateTestCase):
    @tag("tree")
    def test_a_change_of_any_kind_reaches_a_tree_test_that_declares_nothing_and_none_does_not(self):
        """A walk reads a new test module, a template and a doc alike, by no import and no name."""
        modules = [f"{package}/example/tests/test_example.py" for package in SUITE]
        others = ["static/css/app.css", "templates/base.html", "docs/Example.md", ".github/workflows/ci.yml"]
        with everywhere("tree_tests", return_value=[("hq.a.tests.test_tree.Walks", None)]):
            for path in [*modules, *others]:
                with self.subTest(path=path):
                    self.assertIn("hq.a.tests.test_tree.Walks", fast_select.with_tree([], [path]))
            self.assertEqual(fast_select.with_tree([], []), [])

    @tag("tree")
    def test_a_tree_test_that_declares_what_it_reads_runs_on_a_change_to_those_files_alone(self):
        """An edit reaches a file it opens; only an added or deleted file reaches one it lists."""
        walks = "hq.a.tests.test_tree.Walks"
        declared = [(walks, (("templates/**/*.html",), ("hq/**/*.py",)))]
        with everywhere("tree_tests", return_value=declared):
            for paths, listed in (
                (["templates/partials/table.html"], []),
                (["templates/new.html"], ["templates/new.html"]),
                (["hq/domains/example/views.py"], ["hq/domains/example/views.py"]),
            ):
                with self.subTest(paths=paths, listed=listed):
                    self.assertEqual(fast_select.with_tree([], paths, listed), [walks])
            for paths, listed in (
                (["hq/domains/example/views.py"], []),
                (["static/css/app.css", "docs/Example.md"], ["docs/Example.md"]),
            ):
                with self.subTest(paths=paths, listed=listed):
                    self.assertEqual(fast_select.with_tree([], paths, listed), [])

    @tag("tree")
    def test_a_change_adds_each_tree_test_it_reaches_to_the_one_run(self):
        from hq.platform.application.tests.tree import tree_tests

        tree = tree_tests(ROOT)
        undeclared = {label for label, held in tree if held is None}
        self.assertIn("hq.platform.application.tests.test_tree_tag.TreeTagTests", undeclared)
        # The walk above is this checkout's; with_tree asks for it once and adds what it answers.
        with everywhere("tree_tests", return_value=tree) as walked:
            labels = fast_select.with_tree(["hq.a.tests.test_b"], ["hq/domains/example/models.py"], [])
        walked.assert_called_once_with(fast_chain.ROOT)
        self.assertLessEqual({"hq.a.tests.test_b", *undeclared}, set(labels))
        command = fast_gate.run_command((), [], labels, "2")
        self.assertLessEqual(set(labels), set(command))
        self.assertNotIn("--tag", command)
        self.assertNotIn("--exclude-tag", command)
        # A labeled run keeps the source tests a bare whole-suite run leaves out.
        self.assertEqual(excluded_tags(labels, set(), set()), set())

    @tag("tree")
    def test_the_walk_started_beside_the_selection_is_the_one_read(self):
        walks = [("hq.a.tests.test_tree.Walks", None)]
        with everywhere("tree_tests", return_value=walks) as walked:
            walking = fast_select.walk_for_tree()
            labels = fast_select.with_tree(["hq.a.tests.test_b"], ["docs/Example.md"], None, walking)
        walked.assert_called_once_with(fast_chain.ROOT)
        self.assertEqual(labels, ["hq.a.tests.test_b", "hq.a.tests.test_tree.Walks"])

    def test_the_paths_a_listing_can_tell_are_those_added_or_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            git("init", "--quiet")
            for name in ("kept.txt", "edited.txt", "deleted.txt"):
                (root / name).write_text("base\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / "edited.txt").write_text("edited\n")
            (root / "deleted.txt").unlink()
            (root / "staged.txt").write_text("new\n")
            git("add", "staged.txt")
            (root / "untracked.txt").write_text("new\n")
            with patch.object(fast_chain, "ROOT", root):
                listed = fast_select.listed_change(base, ["deleted.txt"])
        self.assertEqual(listed, ["deleted.txt", "staged.txt", "untracked.txt"])

    def test_a_test_both_selected_and_tagged_tree_runs_once(self):
        module = "tools.gate.tests.test_turn"
        once = DiscoverRunner(verbosity=0).build_suite([module])
        twice = DiscoverRunner(verbosity=0).build_suite([module, *(test.id() for test in iter_test_cases(once))])
        self.assertGreater(once.countTestCases(), 1)
        self.assertEqual(twice.countTestCases(), once.countTestCases())

    def test_fast_installs_the_lint_tools_its_gates_and_tests_run(self):
        self.assertIn("setup:lint-tools", mise_tasks()["fast"]["depends"])


@tag("source")
class FastGateNestedLayoutTests(GateTestCase):
    def test_app_config_label_can_differ_from_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/apps.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text(
                'class ExampleConfig:\n    name = "hq.domains.example"\n    label = "stable_label"\n'
            )
            with patch.object(fast_chain, "ROOT", root), everywhere("python_files", return_value=[path]):
                apps = fast_select.django_apps()
            self.assertEqual(apps, {"hq/domains/example": "stable_label"})
            self.assertEqual(fast_select.owner("hq/domains/example/views.py", apps), "stable_label")
            self.assertEqual(fast_select.owner("templates/stable_label/list.html", apps), "stable_label")
            self.assertEqual(fast_select.owner("static/stable_label/site.css", apps), "stable_label")

    def test_an_app_is_each_class_naming_one_in_an_apps_module_under_hq(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = {
                "hq/domains/example/apps.py": (
                    "import os\n"
                    "class Helper:\n    verbose = 3\n"
                    'class ExampleConfig:\n    default_auto_field = os.sep\n    name = "hq.domains.example"\n'
                    'class OtherConfig:\n    name, label = "hq.domains.other", "ignored"\n'
                ),
                "tools/example/apps.py": 'class ToolConfig:\n    name = "tools.example"\n',
            }
            for path, text in modules.items():
                (root / path).parent.mkdir(parents=True)
                (root / path).write_text(text)
            with (
                patch.object(fast_chain, "ROOT", root),
                everywhere("python_files", return_value=[Path(path) for path in modules]),
            ):
                apps = fast_select.django_apps()
            # Without a label of its own, an app is labeled by its name's last part.
            self.assertEqual(apps, {"hq/domains/example": "example"})

    def test_moved_untracked_sources_are_included_and_deleted_sources_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = Path("hq/domains/example/views.py")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text("")
            with (
                patch.object(fast_chain, "ROOT", root),
                everywhere("names", return_value=[str(path), "example/views.py"]),
            ):
                self.assertEqual(fast_select.python_files(), [path])

    def test_a_name_git_quotes_in_a_list_of_lines_reaches_the_selection_as_it_is(self):
        """systemd's escaping writes a backslash into a unit's name, which git quotes unless asked for -z."""

        added, new, gone, module = (rf"deploy/run\x2d{name}" for name in ("added.mount", "new.mount", "gone", "x.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            git("init", "--quiet")
            (root / "deploy").mkdir()
            (root / gone).write_text("gone\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            (root / gone).unlink()
            (root / added).write_text("added\n")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "change")
            (root / new).write_text("new\n")
            (root / module).write_text("")
            with patch.object(fast_chain, "ROOT", root):
                self.assertEqual(fast_gate.changed_paths(base, root), (sorted([added, new, module]), [gone]))
                self.assertEqual(fast_select.python_files(), [Path(module)])

    def test_a_renamed_files_old_path_selects_what_reached_it(self):
        """git sees an unchanged file moved as a rename, which names only its new path unless asked not to."""

        files = {
            "hq/__init__.py": "",
            "hq/domains/__init__.py": "",
            "hq/domains/example/__init__.py": "",
            "hq/domains/example/apps.py": 'class ExampleConfig:\n    name = "hq.domains.example"\n',
            "hq/domains/example/models.py": "class Thing: ...\n",
            "hq/domains/example/moved.py": "VALUE = 1\n",
            "hq/domains/other/__init__.py": "",
            "hq/domains/other/apps.py": 'class OtherConfig:\n    name = "hq.domains.other"\n',
            "deploy/old.yml": "example: true\n",
            "tests/__init__.py": "",
            "tests/test_app.py": "from hq.domains.example.models import Thing\n",
            "tests/test_reads.py": 'PATH = "deploy/old.yml"\n',
            "tests/test_unrelated.py": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = fixture_git(root)
            for path, text in files.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            git("init", "--quiet")
            git("add", "-A")
            git("commit", "--quiet", "--no-verify", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("mv", "hq/domains/example/moved.py", "hq/domains/other/moved.py")
            git("mv", "deploy/old.yml", "deploy/new.yml")
            git("commit", "--quiet", "--no-verify", "-m", "rename")
            with (
                patch.object(fast_chain, "ROOT", root),
                everywhere("merge_base", return_value=base),
                patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                self.assertEqual(fast_gate.main(Usage({"list": "true"})), 0)
        listed = out.getvalue().splitlines()
        self.assertIn("tests.test_app", listed)
        self.assertIn("tests.test_reads", listed)
        self.assertNotIn("tests.test_unrelated", listed)

    def test_only_a_test_named_module_of_a_package_is_a_label(self):
        """A module run in a dependency group's environment, or run as a harness, is no label of the default run."""
        self.assertTrue(fast_select.is_test(Path("tools/gate/tests/test_fast.py")))
        for path in (
            "tests/fuzz/api_properties.py",
            "tests/fuzz/parsers.py",
            "tests/fuzz/__init__.py",
            "tests/fixtures/example_hq_plugin/views.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(fast_select.is_test(Path(path)))


# A test module only a test task's dependency group can import, and that task.
GROUP_MODULE, GROUP_TASK = "tests/fuzz/api_properties.py", "test:api-properties"


@tag("source", "tree")
class FastGateGroupTaskTests(GateTestCase):
    """A test module a test task runs in a dependency group's environment is selected as that task, never as a
    label of the default run, which cannot import it."""

    def run_on(self, changed: str) -> tuple[list[str], list[str]]:
        """The labels fast's test run is given on a change to ``changed``, and the quick gates it runs."""

        recorder = Recorder()
        run_main((changed,), (), recorder, touches_shared_ui=False)
        (run,) = [cmd for _, cmd in recorder.calls if fast_gate.TESTS in cmd]
        tests = run.index(fast_gate.TESTS)
        return run[tests:], run[:tests]

    def test_a_change_that_reaches_the_module_runs_its_task_and_no_label_for_it(self):
        labels, gates = self.run_on(GROUP_MODULE)
        self.assertNotIn(fast_select.module_name(Path(GROUP_MODULE)), labels)
        self.assertIn(GROUP_TASK, gates)

    def test_a_change_that_does_not_reach_the_module_runs_neither(self):
        labels, gates = self.run_on("hq/domains/projects/models.py")
        self.assertNotIn(fast_select.module_name(Path(GROUP_MODULE)), labels)
        self.assertNotIn(GROUP_TASK, gates)


class FastGateRecordingSelectionTests(GateTestCase):
    """A recording mise selects for a change to Python alone runs only where the change selects a test it
    records; any other change keeps mise's selection."""

    def setUp(self):
        tasks = {
            "lint:record": {"name": "lint:record", "tags": ["fast", fast_gate.RECORDING]},
            "lint:unnamed": {"name": "lint:unnamed", "tags": ["fast", fast_gate.RECORDING]},
            "lint:ruff": {"name": "lint:ruff", "tags": ["fast"]},
        }
        held = {"lint:record": ["example.api", "example.contacts.tests"], "lint:unnamed": []}
        self.enterContext(everywhere("quick_tasks", return_value=tasks))
        self.enterContext(everywhere("recorded_labels", side_effect=held.__getitem__))

    def unreached(self, paths: list[str], labels: list[str]) -> list[str]:
        return fast_gate.unreached_recordings(("lint:record", "lint:unnamed", "lint:ruff"), paths, labels)

    def test_a_python_change_that_selects_a_recorded_test_runs_the_recording(self):
        for label in ("example.api.tests.test_calls", "example.contacts.tests.test_form.FormTests"):
            with self.subTest(label=label):
                self.assertEqual(self.unreached(["example/api/views.py"], [label]), [])

    def test_a_python_change_that_selects_none_leaves_it_out_and_every_other_gate_in(self):
        """A label that only begins with a recorded one's name is another package."""
        unreached = self.unreached(["example/turn.py"], ["example.apis.tests.test_x", "example.other.tests"])
        self.assertEqual(unreached, ["lint:record"])

    def test_a_change_beside_python_keeps_mises_selection(self):
        for paths in (["example/api/openapi-examples.json"], ["example/turn.py", "templates/base.html"], []):
            with self.subTest(paths=paths):
                self.assertEqual(self.unreached(paths, []), [])


# What a failing `mise run -c` prints at its end, as it prints it.
FAILING = r"""
import sys
sys.stdout.write("..F")
sys.stdout.flush()
print("\nFinished in 1.0s")
print("ERROR 2 task(s) failed:")
print("[checks] exited with status 1")
print("[lint:ruff] exited with status 1")
sys.exit(1)
"""


# A failure and an error, for the runner to report as it reports them.
RUNNER_FAILURES = """
import unittest


def missing():
    raise LookupError("no example.invalid record")


class Case(unittest.TestCase):
    def test_compares(self):
        self.assertEqual("expected", "found")

    def test_raises(self):
        missing()
"""


# `python -m tools.gate.verdict`, keeping its verdict in the folder named first.
VERDICT_IN = (
    "import sys; from pathlib import Path; from tools.gate import verdict;"
    " verdict.VERDICTS = Path(sys.argv[1]); sys.exit(verdict.main(sys.argv[2:]))"
)


@tag("source", "tree")
class GateVerdictTests(GateTestCase):
    def gate(self, *cmd: str, output: str = "short") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "check", *cmd],
            cwd=ROOT,
            env=os.environ | {"HQ_GATE_OUTPUT": output},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_the_last_line_names_the_tasks_mise_says_failed_and_the_log_keeps_the_output_unchanged(self):
        for output in ("short", "all"):
            with self.subTest(output=output):
                result = self.gate(sys.executable, "-c", FAILING, output=output)
                self.assertEqual(result.returncode, 1)
                *printed, last = result.stdout.splitlines()
                self.assertTrue(last.startswith("[check] FAILED"), last)
                self.assertIn("checks; lint:ruff", last)
                log = Path(gate_verdict.VERDICTS, "check.log").read_text().splitlines()
                for line in ("..F", "[lint:ruff] exited with status 1", *printed, last):
                    self.assertIn(line, log)
                # The progress dots are the command's own output, shown only when asked for all of it.
                self.assertEqual("..F" in printed, output == "all", printed)

    def test_a_short_pass_prints_where_the_run_is_kept_and_its_verdict_alone(self):
        result = self.gate(sys.executable, "-c", "for n in range(3000): print(f'line {n}')")
        self.assertEqual(result.returncode, 0)
        first, kept, last = result.stdout.splitlines()
        log = Path(gate_verdict.VERDICTS, "check.log")
        self.assertIn(str(log), first)
        self.assertIn(str(log), kept)
        self.assertTrue(last.startswith("[check] passed in"), last)
        lines = log.read_text().splitlines()
        self.assertEqual(lines[1:-2], [f"line {n}" for n in range(3000)])

    def test_a_short_failure_shows_each_failed_test_s_last_frame_and_the_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "test_example.py").write_text(RUNNER_FAILURES)
            result = self.gate(sys.executable, "-c", UNITTEST, "discover", "-s", folder, "-t", folder)
        printed = result.stdout.splitlines()
        self.assertTrue(
            printed[-1].endswith(
                "; rerun: mise run test test_example.Case.test_compares test_example.Case.test_raises"
            ),
            printed[-1],
        )
        raised = printed[printed.index("[check] test_example.Case.test_raises ended on:") :]
        self.assertTrue(any(line.endswith(", in missing") for line in raised), raised)
        self.assertTrue(any("LookupError: no example.invalid record" in line for line in raised), raised)
        compared = printed[printed.index("[check] test_example.Case.test_compares ended on:") :]
        # The frame shown is the test's own line, not unittest's assert.
        self.assertTrue(any('self.assertEqual("expected", "found")' in line for line in compared[:3]), compared)
        # The runner's own lines (its dots, its summary) are in the log alone.
        self.assertNotIn("FAILED (failures=1, errors=1)", result.stdout)
        self.assertIn("FAILED (failures=1, errors=1)", Path(gate_verdict.VERDICTS, "check.log").read_text())

    def test_the_verdict_is_kept_whole_in_the_file_it_names(self):
        tests = [f"hq.x.tests.test_y.Case.test_{n}" for n in range(gate_verdict.SHOWN + 2)]
        failed = gate_verdict.Step("tests", ok=False, tasks=["lint:ruff"], tests=[FailedTest(test) for test in tests])
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            gate_verdict.Gate("test at main").close([failed])
        *_, named, last = out.getvalue().splitlines()
        path = Path(named.rsplit(" ", 1)[-1])
        self.assertEqual(path.parent, gate_verdict.VERDICTS)
        _, line, *every = path.read_text().splitlines()
        self.assertEqual(line, last)
        self.assertIn("and 2 more", last)
        self.assertEqual(every, ["lint:ruff", *tests])

    def test_the_last_line_names_the_tests_the_runner_says_failed_and_the_file_says_why(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "test_example.py").write_text(RUNNER_FAILURES)
            result = self.gate(sys.executable, "-c", UNITTEST, "discover", "-s", folder, "-t", folder)
        self.assertEqual(result.returncode, 1)
        last = result.stdout.splitlines()[-1]
        self.assertIn("tests: test_example.Case.test_compares, test_example.Case.test_raises", last)
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text().splitlines()
        self.assertIn("test_example.Case.test_compares: AssertionError: 'expected' != 'found'", kept)
        self.assertIn("test_example.Case.test_raises: LookupError: no example.invalid record", kept)
        traceback = kept[kept.index("test_example.Case.test_raises:") :]
        self.assertTrue(any("missing()" in line for line in traceback), traceback)

    def test_a_pass_says_passed(self):
        result = self.gate(sys.executable, "-c", "print('[lint:ruff] exited with status 1')")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("[check] passed in"))

    def test_a_nested_mise_run_keeps_each_passed_tasks_time(self):
        """Inside a task mise turns its timings off; the verdict turns them back on and keeps them.

        Colored, as mise writes on a CI runner even into a pipe."""
        (ROOT / "var").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as config:
            Path(config, "mise.toml").write_text(
                '[tasks."gate:one"]\nrun = "true"\n[tasks."gate:two"]\nrun = "exit 3"\n'
            )
            mise = [shutil.which("mise") or "mise", "-C", config, "run", "-c", "gate:*"]
            env = {
                **os.environ,
                **trusted_mise(config, Path(config, "state")),
                "MISE_TASK_TIMINGS": "0",
                "CLICOLOR_FORCE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-c", VERDICT_IN, str(gate_verdict.VERDICTS), "check", *mise],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout)
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text().splitlines()
        timed = kept[kept.index("Slowest first:") + 1 :]
        self.assertEqual([line.split()[-1] for line in timed], ["gate:one"])
        self.assertIn("gate:two", kept[: kept.index("Slowest first:")])

    def test_each_time_is_read_in_its_unit_and_kept_slowest_first(self):
        step = gate_verdict.Step("checks")
        # Summary output says a task passed where the default output says it finished.
        for line in ("[a] Finished in 9.3ms", "[b] Finished in 61.01s", "[c] Finished in 12µs", "[d] passed in 1.5s"):
            step.read(f"[checks] {line}")
        self.assertEqual(
            {task: round(seconds, 6) for task, seconds in step.took.items()},
            {"a": 0.0093, "b": 61.01, "c": 0.000012, "d": 1.5},
        )
        ranked = [line.split()[-1] for line in gate_verdict.timings([step])[2:]]
        self.assertEqual(ranked, ["b", "d", "a", "c"])

    def test_steps_run_in_turn_and_a_failure_without_a_task_is_named_by_its_label(self):
        result = self.gate(
            "--step", "Python one", sys.executable, "-c", "pass",
            "--step", "Python two", sys.executable, "-c", "raise SystemExit(1)",
        )  # fmt: skip
        self.assertEqual(result.returncode, 1)
        self.assertIn("[check] Python one", result.stdout.splitlines())
        self.assertIn(": Python two; rerun: ", result.stdout.splitlines()[-1])
        kept = Path(gate_verdict.VERDICTS, "check.txt").read_text()
        self.assertIn("Python one", kept[kept.index("Slowest first:") :])

    def test_a_step_without_its_command_is_refused(self):
        result = self.gate("--step", "Python one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage", result.stderr)

    def test_fast_names_each_failed_step_and_keeps_its_pass_line(self):
        run = gate_verdict.Step(
            "quick gates and tests", ok=False, tasks=["lint:ruff"], tests=[FailedTest("hq.x.tests.test_y.Case.test_z")]
        )
        integrity = gate_verdict.Step("patch integrity", ok=False)
        for steps, expected in (
            ([run, integrity], ": lint:ruff; tests: hq.x.tests.test_y.Case.test_z; patch integrity"),
            ([gate_verdict.Step("quick gates and tests")], "passed in"),
        ):
            with self.subTest(expected=expected):
                code, printed = run_main(selected=("lint:ruff",), step=[*steps, *[gate_verdict.Step("more")] * 3])
                self.assertEqual(code, 0 if expected == "passed in" else 1)
                self.assertIn(expected, printed[-1])


@tag("source", "tree")
class FastGateExtensionSuiteTests(GateTestCase):
    """A shared UI change runs the extensions' suites with a set named, and says it skipped them without one."""

    def run_fast(self, plugins: str | None) -> tuple[list[str], str]:
        recorder = Recorder()
        environ = {key: value for key, value in os.environ.items() if key != "HQ_LOCAL_PLUGINS"}
        if plugins:
            environ["HQ_LOCAL_PLUGINS"] = plugins
        with patch.dict(os.environ, environ, clear=True):
            _, printed = run_main(("templates/example.html",), step=recorder)
        return [label for label, _ in recorder.calls], "\n".join(printed)

    def test_a_named_set_runs_the_extension_suites(self):
        labels, out = self.run_fast("example_plugin.plugin:ExamplePlugin")
        self.assertTrue(any("extension suites" in label for label in labels))
        self.assertNotIn("skipped", out)

    def test_without_a_set_the_skip_is_said_with_the_command_that_names_one(self):
        labels, out = self.run_fast(None)
        self.assertFalse(any("extension suites" in label for label in labels))
        said = [line for line in out.splitlines() if "skipped" in line]
        self.assertEqual(len(said), 1)
        self.assertIn("mise run setup:extensions", said[0])


WRITE = """
import sqlite3
from django.conf import settings
path = settings.DATABASES["default"]["NAME"]
connection = sqlite3.connect(path, timeout=0)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE gate (x)")
connection.commit()
print("database=" + path)
"""


@tag("source")
class GateDatabaseTests(GateTestCase):
    def test_gates_run_side_by_side_each_write_a_database_of_its_own_and_leave_nothing(self):
        # Two processes making one new SQLite file WAL at once: one is refused.
        (ROOT / "var").mkdir(exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=ROOT / "var") as config,
            tempfile.TemporaryDirectory() as scratch,
        ):
            (Path(config) / "write.py").write_text(WRITE)
            run = json.dumps(f"{sys.executable} write.py")
            (Path(config) / "mise.toml").write_text(
                "".join(f'[tasks."gate:{n}"]\nextends = "django"\nrun = {run}\n' for n in range(4))
            )
            # Started outside this run, as a gate is, so each makes a run of its own.
            outside = {name: value for name, value in os.environ.items() if name != RUN}
            result = subprocess.run(
                [shutil.which("mise") or "mise", "run", "-c", "--jobs", "4", "gate:*"],
                cwd=config,
                env={
                    **outside,
                    **trusted_mise(config, Path(config, "state")),
                    "TMPDIR": scratch,
                    "PYTHONPATH": str(ROOT),
                    "DJANGO_SETTINGS_MODULE": "hq.config.settings",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            paths = re.findall(r"database=(\S+)", result.stdout)
            self.assertEqual(len(set(paths)), 4)
            self.assertTrue(all(Path(path).is_relative_to(scratch) for path in paths))
            self.assertEqual(list(Path(scratch).iterdir()), [])
