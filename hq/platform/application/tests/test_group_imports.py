"""A package only a dependency group installs is imported only where that group is.

The image installs the runtime dependencies alone, and its suite loads every
test module it ships to collect it, a source-tagged one too: such a module,
and every module it loads, imports nothing else as it loads. An import inside
a function waits for its call; a lazy one waits for its first use, which in a
test module is discovery itself, since it reads every name the module binds.
A task runs with uv's default groups unless it names another with `uv run
--group`, so only a module such a task runs imports what that group alone
installs.
"""

import importlib.metadata
import itertools
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.facts import Import, imports, package_of
from hq.platform.application.tests.source import source_files
from hq.platform.application.tests.test_mise_config import context_admits
from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
# The browser gates' package: every module in it runs under the browser group.
GROUP_PACKAGES = {"browser": ROOT / "hq" / "platform" / "core" / "tests" / "browser"}
# What a run with a group executes: the labels that open the arguments of
# `-m unittest` or `manage.py test`, or the module of `python -m <module>`,
# when it is this checkout's. A plain `manage.py` runs the command it is given.
RUNS = re.compile(
    r"uv run --group (?P<group>\S+) python (?:(?:-m unittest|manage\.py test) (?P<labels>[^\n]*)|-m (?P<module>\S+))"
)
# The image's copy of the checkout leaves these out, another stage's input only.
IMAGE_COPY = re.compile(r"^COPY ((?:--exclude=\S+ )+)\. /app$", re.MULTILINE)
# What the image's suite collects: Django's discovery pattern, in packages only.
TEST_MODULE = "test*.py"


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
    ``yaml``); one not installed here, which only an optional group brings, is
    read as its name with ``-`` as ``_``. That misses a package whose import
    name differs (graphql-core's ``graphql``) until its group is installed,
    and never the package the group is pinned for.
    """

    recorded: dict[str, set[str]] = {}
    for name, owners in importlib.metadata.packages_distributions().items():
        for owner in owners:
            recorded.setdefault(normalized(owner), set()).add(name)
    return set().union(*(recorded.get(dist, {dist.replace("-", "_")}) for dist in distributions))


def optional_groups() -> list[str]:
    """The dependency groups uv installs only for a run that names one."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    defaults = set(project["tool"]["uv"]["default-groups"])
    return [group for group in project["dependency-groups"] if group not in defaults]


def group_modules(tasks: dict[str, dict]) -> dict[str, set[Path]]:
    """The file of each module a task runs with each group it names."""

    labels: dict[str, set[str]] = {}
    for task in tasks.values():
        for run in RUNS.finditer(body(task)):
            named = labels.setdefault(run["group"], set())
            if run["module"] is None:
                named.update(itertools.takewhile(lambda word: not word.startswith("-"), run["labels"].split()))
            elif (ROOT / run["module"].partition(".")[0]).exists():
                named.add(run["module"])
    return {group: {module_file(label) for label in named} for group, named in labels.items()}


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


def imported(found: list[Import]) -> set[str]:
    """The top-level name of every absolute import in ``found``, at any depth."""

    names = set()
    for each in found:
        if each.source is None:
            names.update(name.partition(".")[0] for name, _ in each.names)
        elif not each.source.startswith("."):
            names.add(each.source.partition(".")[0])
    return names


def loaded_imports(found: list[Import], package: str, *, lazy_waits: bool) -> set[str]:
    """The full name of every module a module in ``package`` loads through its imports, ``found``.

    A function's body runs when it is called, and an import in a ``try`` that
    catches its absence may fail; a lazy import is counted unless ``lazy_waits``;
    one under ``if TYPE_CHECKING:`` never runs.
    """

    waiting = {"called", "guarded", "typed"} | ({"lazy"} if lazy_waits else set())
    return {name for each in found if not each.waits & waiting for name in each.modules(package)}


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


def image_loads(by_module: dict[Path, list[Import]]) -> dict[Path, set[str]]:
    """Each module the image's suite loads to collect its tests, with what it imports as it loads."""

    loads: dict[Path, set[str]] = {}
    tests = set(collected())
    pending = list(tests)
    while pending:
        path = pending.pop()
        if path in loads:
            continue
        loads[path] = loaded_imports(by_module.get(path, []), package_of(path), lazy_waits=path not in tests)
        for name in loads[path]:
            parts = name.split(".")
            pending.extend(
                found for end in range(1, len(parts) + 1) if (found := first_party(".".join(parts[:end]))) is not None
            )
    return loads


@tag("source", "tree")
class GroupImportTests(SimpleTestCase):
    imports: dict[Path, list[Import]]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.lock = Lock(tomllib.loads((ROOT / "uv.lock").read_text()))
        cls.imports = {}
        for each in imports():
            cls.imports.setdefault(each.path, []).append(each)

    def test_only_a_module_a_group_runs_imports_what_that_group_alone_installs(self):
        default = self.lock.installs(self.lock.default_groups)
        runs = group_modules(mise_tasks())
        groups = optional_groups()
        self.assertIn("browser", groups)

        for group in groups:
            with self.subTest(group=group):
                forbidden = import_names(self.lock.installs([*self.lock.default_groups, group]) - default)
                exempt = runs.get(group, set())
                self.assertTrue(forbidden, f"the {group} group installs nothing the default groups do not")
                self.assertTrue(exempt, f"no task runs a module of this checkout with --group {group}")
                package = GROUP_PACKAGES.get(group)

                offenders = sorted(
                    f"{path.relative_to(ROOT)}: {', '.join(sorted(found))}"
                    for path, found_in in self.imports.items()
                    if path not in exempt
                    and not (package and path.is_relative_to(package))
                    and (found := imported(found_in) & forbidden)
                )
                self.assertEqual(
                    offenders, [], f"only the {group} group installs these; a module its tasks run may import them"
                )

    def test_a_module_the_image_suite_loads_imports_only_the_runtime_as_it_loads(self):
        forbidden = import_names(self.lock.installs(list(self.lock.groups)) - self.lock.installs([]))
        loads = image_loads(self.imports)
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

    def test_a_test_the_plugin_check_runs_imports_only_what_the_check_installs(self):
        script = (ROOT / "tools/pipeline/check-plugin.sh").read_text()
        self.assertIn("--group source-tests", script)
        labels = re.findall(r"\b(hq\.[\w.]+)", script.partition('manage.py" test')[2])
        self.assertTrue(labels, "the check runs no host test")
        installed = import_names(self.lock.installs(["source-tests"]))

        offenders = sorted(
            f"{path.relative_to(ROOT)}: {', '.join(sorted(found))}"
            for path in {module_file(label) for label in labels}
            if (found := imported(self.imports.get(path, [])) - installed - set(sys.stdlib_module_names))
            and (found := {name for name in found if first_party(name) is None})
        )
        self.assertEqual(
            offenders, [], "the plugin check installs the runtime and the source-tests group: add the package there"
        )


class LoadedImportTests(SimpleTestCase):
    def test_an_import_that_waits_or_may_fail_is_not_loaded_with_its_module(self):
        source = (
            "import example.eager\n"
            "lazy import example.lazy\n"
            "if TYPE_CHECKING:\n    import example.typed\n"
            "def f():\n    import example.called\n"
            "try:\n    import example.tried\nexcept ImportError:\n    import example.fallback\n"
            "from . import sibling\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.py"
            path.write_text(source)
            found = imports(path)

            def loaded(*, lazy_waits):
                return loaded_imports(found, "example", lazy_waits=lazy_waits)

            self.assertEqual(
                loaded(lazy_waits=True), {"example.eager", "example.fallback", "example", "example.sibling"}
            )
            self.assertEqual(loaded(lazy_waits=False) - loaded(lazy_waits=True), {"example.lazy"})
