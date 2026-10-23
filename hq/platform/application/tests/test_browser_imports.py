"""Only a module the browser group's tasks run imports what that group alone
installs: the suite's other tasks run with uv's default groups, where such an
import fails as the test loads."""

import ast
import itertools
import re
import tomllib
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import parsed, source_files
from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
GROUP = "browser"
# What a browser-group run executes: the labels that open the arguments of
# `-m unittest` or `manage.py test`, or the module of `python -m <module>`,
# when it is this checkout's. A plain `manage.py` runs the command it is given.
RUNS = re.compile(
    rf"uv run --group {GROUP} python (?:(?:-m unittest|manage\.py test) (?P<labels>[^\n]*)|-m (?P<module>\S+))"
)


def browser_only_imports(lock: dict) -> set[str]:
    """The import name of each package the browser group brings that the
    default groups and the project's own dependencies do not.

    A distribution's import name is taken as its name with ``-`` read as
    ``_``, which holds for every package the group brings.
    """

    packages = {package["name"]: package for package in lock["package"]}
    root = next(package for package in lock["package"] if package["source"].get("virtual") == ".")
    groups = root["dev-dependencies"]

    def closure(names: list[dict]) -> set[str]:
        reached: set[str] = set()
        pending = list(names)
        while pending:
            dependency = pending.pop()
            package = packages[dependency["name"]]
            extras = [extra for name in dependency.get("extra", []) for extra in package["optional-dependencies"][name]]
            if dependency["name"] in reached and not extras:
                continue
            reached.add(dependency["name"])
            pending.extend(package.get("dependencies", []) + extras)
        return reached

    default = closure(root["dependencies"] + [dep for group in root["default-groups"] for dep in groups[group]])
    return {name.replace("-", "_") for name in closure(groups[GROUP]) - default}


def browser_modules(tasks: dict[str, dict]) -> set[Path]:
    """The file of each module a task runs with the browser group."""

    labels = set()
    for task in tasks.values():
        for run in RUNS.finditer(body(task)):
            if run["module"] is None:
                labels.update(itertools.takewhile(lambda word: not word.startswith("-"), run["labels"].split()))
            elif (ROOT / run["module"].partition(".")[0]).exists():
                labels.add(run["module"])
    return {module_file(label) for label in labels}


def module_file(label: str) -> Path:
    """The module a test label or ``-m`` name lives in: its longest prefix that is a file."""

    parts = label.split(".")
    for end in range(len(parts), 0, -1):
        path = ROOT.joinpath(*parts[:end])
        for candidate in (path.with_suffix(".py"), path / "__init__.py"):
            if candidate.is_file():
                return candidate
    raise AssertionError(f"{label} names no module in this checkout")


def imported(tree: ast.Module) -> set[str]:
    """The top-level name of every absolute import in ``tree``, at any depth."""

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


@tag("source")
class BrowserImportTests(SimpleTestCase):
    def test_only_a_browser_module_imports_what_the_browser_group_alone_installs(self):
        forbidden = browser_only_imports(tomllib.loads((ROOT / "uv.lock").read_text()))
        exempt = browser_modules(mise_tasks())
        self.assertTrue(forbidden)
        self.assertTrue(exempt)

        offenders = sorted(
            f"{path.relative_to(ROOT)}: {', '.join(sorted(found))}"
            for path in source_files(ROOT, "*.py")
            if path not in exempt and (found := imported(parsed(path)) & forbidden)
        )
        self.assertEqual(
            offenders, [], f"only the {GROUP} group installs these; a module its tasks run may import them"
        )
