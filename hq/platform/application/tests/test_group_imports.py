"""A package only a dependency group installs is imported only where that group is.

The image installs the runtime dependencies alone, and its suite loads every
test module it ships to collect it, a source-tagged one too: such a module,
and every module it loads, imports nothing else as it loads. An import inside
a function waits for its call; a lazy one waits for its first use, which in a
test module is discovery itself, since it reads every name the module binds.
Every task but the browser group's runs with uv's
default groups, so only a module those tasks run imports what the browser
group alone installs.
"""

import ast
import importlib.metadata
import itertools
import re
import tomllib
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import parsed, source_files
from hq.platform.application.tests.test_mise_config import context_admits
from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
GROUP = "browser"
# The browser gates' package: every module in it runs under the browser group.
BROWSER_GATES = ROOT / "hq" / "platform" / "core" / "tests" / "browser"
# What a browser-group run executes: the labels that open the arguments of
# `-m unittest` or `manage.py test`, or the module of `python -m <module>`,
# when it is this checkout's. A plain `manage.py` runs the command it is given.
RUNS = re.compile(
    rf"uv run --group {GROUP} python (?:(?:-m unittest|manage\.py test) (?P<labels>[^\n]*)|-m (?P<module>\S+))"
)
# The image's copy of the checkout leaves these out, another stage's input only.
IMAGE_COPY = re.compile(r"^COPY ((?:--exclude=\S+ )+)\. /app$", re.MULTILINE)
# What the image's suite collects: Django's discovery pattern, in packages only.
TEST_MODULE = "test*.py"
NOT_INSTALLED = (ImportError, ModuleNotFoundError)


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class Lock:
    """What uv.lock installs: the runtime dependencies, and each group's on top."""

    def __init__(self, lock: dict):
        self.packages = {package["name"]: package for package in lock["package"]}
        root = next(package for package in lock["package"] if package["source"].get("virtual") == ".")
        self.runtime = root["dependencies"]
        self.groups: dict[str, list[dict]] = root["dev-dependencies"]
        self.default_groups: list[str] = root["default-groups"]

    def installs(self, groups: list[str]) -> set[str]:
        """The distributions the runtime dependencies and ``groups`` bring, each name normalized."""

        reached: set[str] = set()
        pending = self.runtime + [dependency for group in groups for dependency in self.groups[group]]
        while pending:
            dependency = pending.pop()
            package = self.packages[dependency["name"]]
            extras = [extra for name in dependency.get("extra", []) for extra in package["optional-dependencies"][name]]
            if dependency["name"] in reached and not extras:
                continue
            reached.add(dependency["name"])
            pending.extend(package.get("dependencies", []) + extras)
        return {normalized(name) for name in reached}


def import_names(distributions: set[str]) -> set[str]:
    """The top-level import names of ``distributions``.

    An installed distribution's are what its metadata records (PyYAML's is
    ``yaml``); one not installed here, which only the browser group brings, is
    read as its name with ``-`` as ``_``, which holds for every package that
    group brings.
    """

    recorded: dict[str, set[str]] = {}
    for name, owners in importlib.metadata.packages_distributions().items():
        for owner in owners:
            recorded.setdefault(normalized(owner), set()).add(name)
    return set().union(*(recorded.get(dist, {dist.replace("-", "_")}) for dist in distributions))


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

    found = first_party(label)
    if found is None:
        raise AssertionError(f"{label} names no module in this checkout")
    return found


def first_party(name: str) -> Path | None:
    """The file of this checkout's module ``name`` or its longest prefix; None for another distribution's."""

    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        path = ROOT.joinpath(*parts[:end])
        for candidate in (path.with_suffix(".py"), path / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def imported(tree: ast.Module) -> set[str]:
    """The top-level name of every absolute import in ``tree``, at any depth."""

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.partition(".")[0])
    return names


def guards_a_missing_package(node: ast.Try) -> bool:
    """Whether a ``try`` carries on when its imports are not installed."""

    caught = [handler.type for handler in node.handlers]
    names = {
        element.id for kind in caught if kind is not None for element in ast.walk(kind) if isinstance(element, ast.Name)
    }
    return None in caught or bool(names & {error.__name__ for error in NOT_INSTALLED})


def loaded_imports(path: Path, *, lazy_waits: bool) -> set[str]:
    """The full name of every module ``path`` imports as it loads.

    A function's body runs when it is called, and an import in a ``try`` that
    catches its absence may fail; a lazy import is counted unless ``lazy_waits``.
    """

    package = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    names: set[str] = set()
    pending: list[ast.AST] = [parsed(path)]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Try) and guards_a_missing_package(node):
            pending.extend(node.handlers + node.orelse + node.finalbody)
            continue
        if isinstance(node, ast.Import | ast.ImportFrom) and node.is_lazy and lazy_waits:
            continue
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.split(".")[: len(package.split(".")) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            names.update(f"{module}.{alias.name}" for alias in node.names)
        pending.extend(ast.iter_child_nodes(node))
    return names


def collected() -> list[Path]:
    """The test modules the image's suite collects: those its /app holds that
    discovery reaches, through packages only."""

    ignore = (ROOT / "Dockerfile.dockerignore").read_text().splitlines()
    copy = IMAGE_COPY.search((ROOT / "Dockerfile").read_text())
    excluded = re.findall(r"--exclude=(\S+)", copy[1]) if copy else []

    def reached(path: Path) -> bool:
        parts = path.relative_to(ROOT).parts
        return (
            context_admits(ignore, path.relative_to(ROOT).as_posix())
            and parts[0] not in excluded
            and all((ROOT.joinpath(*parts[:end]) / "__init__.py").is_file() for end in range(1, len(parts)))
        )

    return [path for path in source_files(ROOT, TEST_MODULE) if reached(path)]


def image_loads() -> dict[Path, set[str]]:
    """Each module the image's suite loads to collect its tests, with what it imports as it loads."""

    loads: dict[Path, set[str]] = {}
    tests = set(collected())
    pending = list(tests)
    while pending:
        path = pending.pop()
        if path in loads:
            continue
        loads[path] = loaded_imports(path, lazy_waits=path not in tests)
        for name in loads[path]:
            parts = name.split(".")
            pending.extend(
                found for end in range(1, len(parts) + 1) if (found := first_party(".".join(parts[:end]))) is not None
            )
    return loads


@tag("source", "tree")
class GroupImportTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lock = Lock(tomllib.loads((ROOT / "uv.lock").read_text()))

    def test_only_a_browser_module_imports_what_the_browser_group_alone_installs(self):
        default = self.lock.installs(self.lock.default_groups)
        forbidden = import_names(self.lock.installs([*self.lock.default_groups, GROUP]) - default)
        exempt = browser_modules(mise_tasks())
        self.assertTrue(forbidden)
        self.assertTrue(exempt)

        offenders = sorted(
            f"{path.relative_to(ROOT)}: {', '.join(sorted(found))}"
            for path in source_files(ROOT, "*.py")
            if path not in exempt
            and not path.is_relative_to(BROWSER_GATES)
            and (found := imported(parsed(path)) & forbidden)
        )
        self.assertEqual(
            offenders, [], f"only the {GROUP} group installs these; a module its tasks run may import them"
        )

    def test_a_module_the_image_suite_loads_imports_only_the_runtime_as_it_loads(self):
        forbidden = import_names(self.lock.installs(list(self.lock.groups)) - self.lock.installs([]))
        loads = image_loads()
        self.assertIn("yaml", forbidden)
        self.assertIn(Path(__file__).resolve(), loads)

        offenders = sorted(
            f"{path.relative_to(ROOT)}: {', '.join(sorted(found))}"
            for path, names in loads.items()
            if (found := {name.partition(".")[0] for name in names} & forbidden)
        )
        self.assertEqual(
            offenders,
            [],
            "the image installs only the runtime dependencies: import these where they are used",
        )
