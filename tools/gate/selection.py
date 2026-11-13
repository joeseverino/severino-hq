"""What fast selects from a change: the test modules it reaches through
imports, apps, the paths modules name and the map of the tests that ran each
file (docs/Development.md, What `fast` runs)."""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from hq.platform.application.tests.reads import Reads
from hq.platform.application.tests.tree import TreeTests, tree_tests
from hq.platform.core.tests.task import changed_paths, git, names
from tests.mise_tasks import ROOT as TASKS_ROOT, body, mise_tasks, run_files, runs
from tools.gate import chain, testmap
from tools.gate.chain import maps, recorded_ancestor

# Never part of the Django suite: other languages and vendored trees.
SKIP_PARTS = {"node_modules", "controller", ".venv", "staticfiles"}
# Run whatever changed. The startup budgets count what settings and
# django.setup import, so an import added anywhere they reach is one the
# test's own imports never select.
ALWAYS = ("architecture", "new_domain", "test_startup")
# The module every test that reads the tasks goes through; a change to a task
# definition, or to a file a task runs, reaches those tests as a change to it would.
TASK_READER = "tests.mise_tasks"
# A test task that runs its labels in a dependency group's environment, which
# the default one fast tests in lacks: a module it names is selected as that
# task, never as a label.
GROUP_TASK = "test:"
GROUP_RUN = re.compile(r"\buv run\b[^\n]*--group\b[^\n]*\bmanage\.py test\b((?:\s+[\w.]+)+)")
# How many importers away from a changed module a test is still selected: one
# that imports it, or imports a module that does. Further out the walk reaches
# most of the tree through the shared layers, and the module in between has
# tests of its own at this distance.
REACH = 2


def defines_tasks(path: str) -> bool:
    """A file mise reads this checkout's tasks from (mise.toml, or one in
    mise-tasks/, mise's default task directory), or one a task's run names,
    which the tests that read the tasks read with it."""

    return path == "mise.toml" or path.startswith("mise-tasks/") or path in run_files()


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def python_files() -> list[Path]:
    found = set(names("ls-files", "--cached", "--others", "--exclude-standard", "*.py", root=chain.ROOT))
    return sorted(Path(f) for f in found if (chain.ROOT / f).exists() and not SKIP_PARTS & set(Path(f).parts))


def is_test(path: Path) -> bool:
    """A test module the suite discovers: every parent directory is a package.

    A test outside one (the structural bar's) belongs to its own gate.
    """

    return (
        path.suffix == ".py"
        and path.name != "__init__.py"
        and path.name.startswith("test")
        and all((chain.ROOT / parent / "__init__.py").exists() for parent in path.parents if parent != Path())
    )


def packages(module: str, modules: set[str]) -> set[str]:
    """The module and the package __init__ files above it, which importing it also runs."""

    pieces = module.split(".")
    return {".".join(pieces[:n]) for n in range(1, len(pieces) + 1)} & modules


def importers(files: list[Path]) -> dict[str, set[str]]:
    """module -> modules that import it, read once per tree and file list, so
    the tests that map many changes read the tree once."""

    return _importers(chain.ROOT, tuple(files))


@functools.cache
def _importers(root: Path, files: tuple[Path, ...]) -> dict[str, set[str]]:
    """Ruff's import graph, which reads a test's dotted strings (mock targets)
    as imports too. A string outside a test is a late-bound reference, such
    as a domain's declaration naming its urlconf: followed, the registry
    would reach every app and every change would select the whole suite."""

    modules = {module_name(p) for p in files}
    reverse: dict[str, set[str]] = {m: set() for m in modules}
    tests = [p for p in files if is_test(p)]
    # Ruff shortens a string that names an attribute (a mock target) down to
    # --min-dots dots, so at 1 `package.module.attribute` reaches its module.
    strings = import_graph(tests, "--detect-string-imports", "--min-dots", "1")
    for graph in (import_graph(list(files)), strings):
        for source, targets in graph.items():
            me = module_name(Path(source))
            for target in targets:
                for module in packages(module_name(Path(target)), modules) - {me}:
                    reverse[module].add(me)
    return reverse


def import_graph(files: list[Path], *flags: str) -> dict[str, list[str]]:
    """Each file and the files of this tree it imports, as `ruff analyze graph` reads them."""

    if not files:
        return {}
    found = subprocess.run(
        [sys.executable, "-m", "ruff", "analyze", "graph", "--quiet", *flags, *map(str, files)],
        cwd=chain.ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(found.stdout)


def django_apps(files: list[Path] | None = None) -> dict[str, str]:
    """Actual AppConfig source prefixes mapped to their stable Django labels."""

    apps: dict[str, str] = {}
    for path in python_files() if files is None else files:
        if path.name == "apps.py" and path.parts[0] == "hq":
            apps.update(app_configs(ast.parse((chain.ROOT / path).read_text(encoding="utf-8"))))
    return apps


def app_configs(tree: ast.Module) -> dict[str, str]:
    """Each class in a module that names an app: its name as a source prefix,
    mapped to its label, or to its name's last part without one."""

    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        values = string_attributes(node)
        if name := values.get("name"):
            found[name.replace(".", "/")] = values.get("label", name.rsplit(".", 1)[-1])
    return found


def string_attributes(node: ast.ClassDef) -> dict[str, str]:
    """The class's attributes assigned a string literal, by name."""

    return {
        target.id: assignment.value.value
        for assignment in node.body
        if isinstance(assignment, ast.Assign)
        and isinstance(assignment.value, ast.Constant)
        and isinstance(assignment.value.value, str)
        for target in assignment.targets
        if isinstance(target, ast.Name)
    }


def owner(path: str, apps: dict[str, str]) -> str | None:
    """Map source directories and shared asset namespaces to app labels."""

    parts = Path(path).parts
    if parts and parts[0] in ("templates", "static") and len(parts) > 2:
        return parts[1] if parts[1] in apps.values() else None
    for prefix, label in apps.items():
        if path == prefix or path.startswith(prefix + "/"):
            return label
    return None


def affected(
    changed: list[str], deleted: list[str], files: list[Path], apart: frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    """(modules reachable from the change through importers, apps it touches).

    A change in an app also selects the app's own tests and every test that
    imports any of the app: a test that drives its views through the client
    imports its models, never its views. A path in ``apart`` touches no app:
    the map names the tests that ran it, or it is a test module (``by_map``).
    """

    reverse = importers(files)
    apps = django_apps(files)
    paths = [*changed, *deleted]
    touched = {label for path in paths if path not in apart and (label := owner(path, apps))}
    return app_reach(touched, apps, reverse) | import_reach(seeds(paths), reverse) | reading(paths, files), touched


def seeds(paths: list[str]) -> set[str]:
    """The modules a change starts from: each Python module it touched, and
    the module that reads the tasks when it touched a task's definition."""

    found = {module_name(Path(path)) for path in paths if path.endswith(".py")}
    return (found | {TASK_READER}) if any(defines_tasks(path) for path in paths) else found


def reading(paths: list[str], files: list[Path]) -> set[str]:
    """The modules a change reaches through the files their source names
    (docs/Development.md, What `fast` runs, says how a path is named), so a
    test that reads a workflow, a template or a doc as text runs when it
    changes. The guard in test_fast.py holds this to every path a test module
    builds (``hq.platform.application.tests.tree.Named``)."""

    index = Naming(named(chain.ROOT, tuple(files)))
    # A module that names a file, and a test that imports it, one hop out: two reach much of the suite.
    near: set[str] = set()
    # A test that names a directory above a module: the modules that name a package are many.
    alone: set[str] = set()
    for parts in (Path(path).parts for path in paths):
        if parts[-1].endswith(".py"):
            near |= index(parts)
            alone |= {module for n in range(2, len(parts)) for module in index(parts[:n])}
        else:
            near |= index.file(parts)
    as_modules = {module_name(Path(path)) for path in near}
    return import_reach(as_modules, importers(files), 1) | {module_name(Path(path)) for path in alone}


class Naming:
    """The files that name a path, read from the index ``named`` builds."""

    def __init__(self, index: dict[tuple[tuple[str, ...], bool], set[str]]):
        self.index = index

    def __call__(self, parts: tuple[str, ...], *, joined: bool = False) -> set[str]:
        """The files that spell ``parts``, or only those that join it to a path before it."""

        return self.index.get((parts, True), set()) | (set() if joined else self.index.get((parts, False), set()))

    def file(self, parts: tuple[str, ...]) -> set[str]:
        """The files that name a file that is not Python: by its path or a directory above it, from the root or
        from a directory above the naming file, and by the name Django loads it by."""

        # The loader and the finders name a file from its directory, wherever it lies, from any module.
        loaded = [n for n, part in enumerate(parts[:-1]) if part in LOADED]
        found = self(parts[loaded[-1] + 1 :]) if loaded else set()
        for n in range(len(parts)):
            scope, rest = "/".join(parts[:n]), parts[n:]
            # A directory of one part is a common word and a URL's first segment ("docs/"), so it counts joined.
            above = {module for k in range(1, len(rest)) for module in self(rest[:k], joined=k == 1)}
            found |= {module for module in self(rest) | above if not scope or module.startswith(f"{scope}/")}
        return found


# The directories Django's template loaders and staticfiles finders read,
# which name a file by its path below them ("partials/table.html").
LOADED = ("templates", "static")
# A run of string literals joined by `/` (`root / "a" / "b.yml"`) or listed
# as arguments (`Path("a", "b.yml")`), and the `/` that joins it to a path
# before it, if one does.
LITERALS = re.compile(r"""(/\s*)?(["'][^"'\n]*["'](?:\s*[/,]\s*["'][^"'\n]*["'])*)""")
LITERAL = re.compile(r"""["']([^"'\n]*)["']\s*([/,]?)""")


@functools.cache
def named(root: Path, files: tuple[Path, ...]) -> dict[tuple[tuple[str, ...], bool], set[str]]:
    """(the parts of a path, whether a `/` joins it to a path before it) -> the
    files whose source spells it, read once per tree and file list."""

    index: dict[tuple[tuple[str, ...], bool], set[str]] = {}
    for path in files:
        for joined, literals in LITERALS.findall((root / path).read_text(encoding="utf-8")):
            for parts in spelled(literals):
                index.setdefault((parts, bool(joined)), set()).add(path.as_posix())
    return index


def spelled(literals: str) -> list[tuple[str, ...]]:
    """The paths a run of literals spells: the whole run, and each run of the
    arguments from the first, since `source_files(root / "a", "*.js")` names
    the directory "a"."""

    parts: list[str] = []
    found = []
    for literal, joiner in LITERAL.findall(literals):
        parts += [part for part in literal.split("/") if part]
        if joiner != "/" and parts:
            found.append(tuple(parts))
    return found


def app_reach(touched: set[str], apps: dict[str, str], reverse: dict[str, set[str]]) -> set[str]:
    """Every module of the touched apps, and each module that imports one.

    One hop only: a test that imports the app. Following importers further
    would reach every module through the shared layers.
    """

    prefixes = [prefix.replace("/", ".") for prefix, label in apps.items() if label in touched]
    members = {m for m in reverse if any(m == prefix or m.startswith(prefix + ".") for prefix in prefixes)}
    return members | {i for m in members for i in reverse[m]}


def import_reach(start: set[str], reverse: dict[str, set[str]], hops: int = REACH) -> set[str]:
    """``start`` and the modules that import it, ``hops`` out."""

    reached = frontier = set(start)
    for _ in range(hops):
        frontier = {i for m in frontier for i in reverse.get(m, ())} - reached
        reached |= frontier
    return reached


def test_labels(files: list[Path], reached: set[str], touched: set[str]) -> tuple[list[str], int]:
    """The test modules to run: those ``chosen`` picks, and the architecture tests."""

    tests = {module_name(p): p for p in files if is_test(p)}
    picked = chosen(tests, reached, touched) | {t for t in tests if any(a in t for a in ALWAYS)}
    return sorted(picked), len(tests)


def by_map(labels: list[str], files: list[Path]) -> list[str]:
    """The selection read from the nearest map of the tests that ran each
    file, when it is narrower than ``labels``, and ``labels`` otherwise; both
    are sound, so the run takes the one with fewer modules.

    The change is read from the map's tree, which passed the whole suite. A
    Python file the map holds whose change stays inside function bodies
    (``testmap.bodies_only``) selects the test modules that ran it there, and
    no app; so does a test module, which no other test runs, only imports.
    Every other path selects as without a map. A test module the recording
    did not run (new since, or skipped there) selects by every rule."""

    found = recorded_ancestor(maps(chain.ROOT), ".json", chain.ROOT)
    if not found:
        return labels
    commit, back = found
    tree = git("rev-parse", f"{commit}^{{tree}}", root=chain.ROOT)[0]
    the_map = testmap.load(maps(chain.ROOT) / f"{tree}.json")
    changed, deleted = changed_paths(commit, chain.ROOT)
    mapped = frozenset(path for path in changed if confined(path, commit, the_map))
    tests = {module_name(p) for p in files if is_test(p)}
    apart = mapped | {path for path in [*changed, *deleted] if is_test(Path(path))}
    reached, touched = affected(changed, deleted, files, apart)
    picked = set(test_labels(files, reached, touched)[0])
    picked |= {module for path in mapped for module in the_map.tests_of(path)} & tests
    if apart:
        unran = set(test_labels(files, *affected(changed, deleted, files))[0]) - the_map.ran
        picked |= unran
    narrower = len(picked) < len(labels)
    print(
        f"[fast] the map of the tests that ran each file, {back} back: {len(mapped)} of the"
        f" {len(changed) + len(deleted)} files changed since change only bodies it saw run;"
        f" {len(picked)} test modules from it, {'taken' if narrower else 'not narrower'}"
    )
    return sorted(picked) if narrower else labels


def confined(path: str, commit: str, the_map: testmap.TestMap) -> bool:
    """Whether ``path`` is a Python file the map holds, changed since ``commit`` only inside function bodies."""

    if not path.endswith(".py") or not the_map.tests_of(path):
        return False
    before = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{path}"], cwd=chain.ROOT, capture_output=True, check=False
    )
    if before.returncode:
        return False
    return testmap.bodies_only(before.stdout.decode(), (chain.ROOT / path).read_text(encoding="utf-8"))


def whole_suite(files: list[Path]) -> list[str]:
    """Every test module, which a wide change runs."""

    return sorted(module_name(p) for p in files if is_test(p))


def chosen(modules: dict[str, Path], reached: set[str], touched: set[str]) -> set[str]:
    """Those of ``modules`` the change reaches, and those that name an app it touched."""

    picked = {m for m in modules if m in reached}
    # A test that names an app exercises it: a URL namespace ("projects:list"),
    # a model label ("projects.Project") or the app label given to Django.
    words = [
        re.compile(
            rf"""["']{re.escape(app)}(?::|\.[A-Z])"""
            rf"""|(?:app_label\W+|get_model\(\s*|get_app_config\(\s*)["']{re.escape(app)}["']"""
        )
        for app in touched
    ]
    for name, path in modules.items():
        if name not in picked and any(word.search((chain.ROOT / path).read_text(encoding="utf-8")) for word in words):
            picked.add(name)
    return picked


@functools.cache
def group_tests() -> dict[str, str]:
    """Each test module a test task runs in a dependency group's environment, by its label: module -> task."""

    found: dict[str, str] = {}
    for name, task in mise_tasks().items():
        run_labels = GROUP_RUN.search(body(task)) if name.startswith(GROUP_TASK) else None
        if not run_labels:
            continue
        labels = run_labels[1].split()
        for path in runs(task):
            module = module_name(path.relative_to(TASKS_ROOT))
            if any(label == module or label.startswith(module + ".") for label in labels):
                found[module] = name
    return found


def group_tasks(files: list[Path], reached: set[str], touched: set[str]) -> tuple[str, ...]:
    """The test tasks whose group-run modules the change reaches as it would reach a test module."""

    present = {module_name(p): p for p in files}
    modules = {m: present[m] for m in group_tests() if m in present}
    picked = chosen(modules, reached, touched)
    return tuple(sorted({group_tests()[m] for m in picked}))


def with_tree(
    labels: list[str],
    paths: list[str],
    listed: list[str] | None = None,
    walking: Future[TreeTests] | None = None,
) -> list[str]:
    """The selected labels, and each test tagged tree (test_tree_tag.py) that ``paths`` reach, of those
    ``walking`` finds or else of a walk run now: a walk finds a file by no import or name, so one that declares
    nothing runs on any change, and one that declares its reads (reads.py) on a change to them, ``listed``
    (``paths`` when not given) being what was added or deleted."""

    if not paths:
        return labels
    found = walking.result() if walking else tree_tests(chain.ROOT)
    names = paths if listed is None else listed
    reached = [label for label, held in found if held is None or Reads(*held).selects(paths, names)]
    print(f"[fast] {len(reached)} of the {len(found)} tests tagged tree run: those the change reaches")
    return [*labels, *reached]


def listed_change(base: str, deleted: list[str]) -> list[str]:
    """The paths a change adds or deletes since ``base``, untracked ones included: all a listing can tell."""

    added = names("diff", "--name-only", "--no-renames", "--diff-filter=A", base, root=chain.ROOT)
    return sorted({*added, *names("ls-files", "--others", "--exclude-standard", root=chain.ROOT), *deleted})


def walk_for_tree() -> Future[TreeTests]:
    """The walk for the tests tagged tree, started now on a thread: it runs in a
    process of its own (``apart``), so it overlaps the reading of the import
    graph and the paths the modules name, which it does not depend on."""

    pool = ThreadPoolExecutor(max_workers=1)
    walking = pool.submit(tree_tests, chain.ROOT)
    # Leaving the pool's block would wait for the walk; this lets it run on.
    pool.shutdown(wait=False)
    return walking


# The module every test that reads each of the browser gate's modules goes
# through. Such a test is a source check that starts no browser (the probe
# check among them), so those selected run ahead of the rest, in fast:source,
# and a refusal there is printed before the suite starts.
BROWSER_MODULES = "hq.platform.core.tests.browser_modules"


def source_first(labels: list[str], files: list[Path]) -> tuple[list[str], list[str]]:
    """(the labels of the tests that read each browser module, the rest)."""

    readers = importers(files).get(BROWSER_MODULES, set())
    first = [label for label in labels if any(label == m or label.startswith(m + ".") for m in readers)]
    return first, [label for label in labels if label not in first]


def selection(
    changed: list[str], deleted: list[str], files: list[Path]
) -> tuple[list[str], int, tuple[set[str], set[str]]]:
    """(the test modules to run, how many there are, what the change reaches and the apps it touches),
    from the base and from the nearest map, whichever selects fewer."""

    reached, touched = affected(changed, deleted, files)
    labels, total = test_labels(files, reached, touched)
    if changed or deleted:
        labels = by_map(labels, files)
    return labels, total, (reached, touched)
