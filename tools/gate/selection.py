"""What fast selects from a change: the test modules it reaches through
imports, the routes and paths modules name and the map of the tests that ran
each file (docs/Development.md, What `fast` runs)."""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from hq.platform.application.tests.reads import Reads
from hq.platform.application.tests.tree import TreeTests, tree_tests
from hq.platform.core.tests.task import RefError, changed_paths, git, names
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


def affected(
    changed: list[str], deleted: list[str], files: list[Path], apart: frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    """(modules the change reaches, the apps whose models it reaches).

    It reaches the modules that import what it touched, those that name a file it touched, and those that
    name a route whose view it reaches, by its name or its path: a test that drives a view through the
    client imports its models, never its views. A path in ``apart`` reaches no route: the map names the
    tests that ran it, or it is a test module (``by_map``).
    """

    reverse = with_gone(importers(files), deleted, files)
    paths = [*changed, *deleted]
    reached = import_reach(seeds(paths), reverse) | reading(paths, files)
    own = [path for path in paths if path not in apart]
    started = seeds(own)
    imported = import_reach(started, reverse)
    read = reading(own, files)
    index = named(chain.ROOT, tuple(files))
    shown = [path for path in rendering(own) if not path.endswith(".py")]
    # A module that names a page a change reaches reaches the routes of the views in it that name the page.
    readers = {
        module_name(Path(path)): path for parts in (Path(p).parts for p in shown) for path in Naming(index).file(parts)
    }
    every = routes(chain.ROOT, tuple(files))
    hit = {
        route
        for route in every
        if route.urls in started
        or route.view in imported
        or (route.view in read and route.view not in readers)
        or (route.view in readers and route.definition in defined_naming(readers[route.view], tuple(shown)))
    }
    near = imported | read
    by_name = {path for route in hit for path in Naming(index)((route.name,))}
    by_path = {path for parts, paths in requested(index, every).items() if parts in hit for path in paths}
    touched = {label for prefix, label in django_apps(files).items() if f"{prefix.replace('/', '.')}.models" in near}
    return reached | {module_name(Path(path)) for path in by_name | by_path}, touched


def requested(index: dict[tuple[tuple[str, ...], bool], set[str]], every: tuple[Route, ...]) -> dict[Route, set[str]]:
    """The files that spell a path each route answers (``/infrastructure/machines/{name}/?q=a``), each path
    resolved as Django resolves it, to the first route whose shape it fits."""

    shaped = [route for route in every if route.path is not None]
    found: dict[Route, set[str]] = {}
    for (parts, _), paths in index.items():
        if not parts:
            continue
        # A query is no part of the path: after a slash it spells a part of its own.
        asked = tuple(part for part in (*parts[:-1], parts[-1].partition("?")[0]) if part)
        route = next((route for route in shaped if fits(asked, route.path)), None)
        if route is not None:
            found.setdefault(route, set()).update(paths)
    return found


def fits(asked: tuple[str, ...], shape: tuple[str | None, ...] | None) -> bool:
    return (
        shape is not None
        and len(shape) == len(asked)
        and all(want is None or want == got for want, got in zip(shape, asked, strict=True))
    )


def seeds(paths: list[str]) -> set[str]:
    """The modules a change starts from: each Python module it touched, the
    models of the app whose migration it touched, and the module that reads
    the tasks when it touched a task's definition."""

    found = {module_name(Path(path)) for path in paths if path.endswith(".py")}
    found |= {
        ".".join(Path(path).parts[: Path(path).parts.index(MIGRATIONS)]) + ".models"
        for path in paths
        if MIGRATIONS in Path(path).parts[:-1]
    }
    return (found | {TASK_READER}) if any(defines_tasks(path) for path in paths) else found


# A migration changes its app's models as the database holds them, which no module imports.
MIGRATIONS = "migrations"
# The functions a URL module declares a route with, and the setting that names the root one.
ROUTE_CALLS = ("path", "re_path")
ROOT_URLCONF = "ROOT_URLCONF"


class Route(NamedTuple):
    """A route a URL module declares by name: its name as a test reverses it ("namespace:name"), the URL
    module, the module its view is defined in, and its path from the root (None for a segment a converter
    fills), when a mount and a plain pattern give one."""

    name: str
    urls: str
    view: str
    definition: str | None
    path: tuple[str | None, ...] | None


@functools.cache
def routes(root: Path, files: tuple[Path, ...]) -> tuple[Route, ...]:
    """Every route the URL modules of a tree declare by name."""

    modules = {module_name(p) for p in files}
    trees = {}
    for path in files:
        text = (root / path).read_text(encoding="utf-8")
        if any(word in text for word in ("urlpatterns", ROOT_URLCONF, "Mount(", "include(")):
            trees[path] = ast.parse(text)
    mounts = mounted(trees.values(), modules)
    found = []
    for path, tree in trees.items():
        urls = module_name(path)
        bound = imported_names(tree, urls, path.name == "__init__.py")
        namespace = next(
            (
                node.value.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "app_name" for t in node.targets)
                and isinstance(node.value, ast.Constant)
            ),
            None,
        )
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and call_name(node.func) in ROUTE_CALLS and len(node.args) > 1):
                continue
            name = next(
                (k.value.value for k in node.keywords if k.arg == "name" and isinstance(k.value, ast.Constant)), None
            )
            if not isinstance(name, str):
                continue
            pattern = node.args[0]
            shape = None
            if urls in mounts and call_name(node.func) == "path" and isinstance(pattern, ast.Constant):
                shape = segments(mounts[urls] + str(pattern.value))
            view, definition = defining_module(node.args[1], bound, modules, urls)
            found.append(Route(f"{namespace}:{name}" if namespace else name, urls, view, definition, shape))
    return tuple(found)


def mounted(trees: Iterable[ast.Module], modules: set[str]) -> dict[str, str]:
    """The prefix each URL module hangs off the root by: none for the one the settings name the root, and
    for another a call that pairs a prefix with its dotted name
    (``Mount("infrastructure/", "hq.domains.control_plane.urls")``, ``path("api/", include("..."))``)."""

    found = {}
    for tree in trees:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == ROOT_URLCONF for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and node.value.value in modules
            ):
                found[node.value.value] = ""
            if not (isinstance(node, ast.Call) and len(node.args) > 1):
                continue
            prefix, target = node.args[:2]
            if isinstance(target, ast.Call) and call_name(target.func) == "include" and target.args:
                target = target.args[0]
            if (
                isinstance(prefix, ast.Constant)
                and isinstance(prefix.value, str)
                and isinstance(target, ast.Constant)
                and target.value in modules
            ):
                found[target.value] = prefix.value
    return found


def segments(path: str) -> tuple[str | None, ...]:
    """A route's path as the parts a spelled path splits into, None for each a converter fills."""

    return tuple(None if "<" in part else part for part in path.split("/") if part)


def call_name(func: ast.expr) -> str | None:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def imported_names(tree: ast.Module, module: str, package: bool) -> dict[str, str]:
    """Each name a module's imports bind, mapped to the dotted name it binds."""

    here = module.split(".") if package else module.split(".")[:-1]
    bound: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            base = here[: len(here) - node.level + 1] if node.level else []
            source = ".".join([*base, *([node.module] if node.module else [])])
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{source}.{alias.name}"
    return bound


def defining_module(view: ast.expr, bound: dict[str, str], modules: set[str], here: str) -> tuple[str, str | None]:
    """(the module of this tree a route's view expression comes from, the definition in it), as
    ``views.ListView.as_view()`` gives (``<app>.views``, ``ListView``); ``here`` when it is defined there or
    comes from outside the tree."""

    chain_: list[str] = []
    node = view
    while not isinstance(node, ast.Name):
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            chain_.insert(0, node.attr)
            node = node.value
        else:
            return here, None
    if node.id not in bound:
        return here, node.id
    dotted = [*bound[node.id].split("."), *chain_]
    for n in range(len(dotted), 0, -1):
        if ".".join(dotted[:n]) in modules:
            return ".".join(dotted[:n]), dotted[n] if n < len(dotted) else None
    return here, None


@functools.cache
def defined_naming(path: str, shown: tuple[str, ...]) -> frozenset[str | None]:
    """The top-level definitions of the module at ``path`` that name one of ``shown`` as a module names a
    file, and the classes in it that derive from one of those."""

    text = (chain.ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    definitions = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    index: dict[tuple[tuple[str, ...], bool], set[str]] = {}
    for node in definitions:
        for joined, literals in LITERALS.findall(ast.get_source_segment(text, node) or ""):
            for parts in spelled(literals):
                index.setdefault((parts, bool(joined)), set()).add(node.name)
    found = {name for path in shown for name in Naming(index).file(Path(path).parts)}
    grew = True
    while grew:
        heirs = {
            node.name
            for node in definitions
            if isinstance(node, ast.ClassDef) and any(isinstance(b, ast.Name) and b.id in found for b in node.bases)
        }
        grew = not heirs <= found
        found |= heirs
    return frozenset(found)


def reading(paths: list[str], files: list[Path]) -> set[str]:
    """The modules a change reaches through the files their source names
    (docs/Development.md, What `fast` runs, says how a path is named), so a
    test that reads a workflow, a template or a doc as text runs when it
    changes. The guard in test_fast.py holds this to every path a test module
    builds (``hq.platform.application.tests.tree.Named``)."""

    index = Naming(named(chain.ROOT, tuple(files)))
    paths = rendering(paths)
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


def rendering(paths: list[str]) -> list[str]:
    """``paths``, and each template that includes or extends one of them, or loads a tag library one of them
    is, as far as that goes: a page drawn from a changed part reaches the views that render the page."""

    naming, loading = templates(chain.ROOT)
    found = list(paths)
    for path in found:
        parts = Path(path).parts
        users = naming.get(loader_name(parts), set()) if TEMPLATES in parts[:-1] else set()
        if parts[-2:-1] == ("templatetags",) and path.endswith(".py"):
            users = users | loading.get(Path(path).stem, set())
        found += sorted(users - set(found))
    return found


# The directory Django's loaders read templates from, and how a template names
# another ({% include "a/b.html" %}, {% extends %}) and loads a tag library.
TEMPLATES = "templates"
TEMPLATE_NAME = re.compile(r"""["']([^"'\s{}%]+\.(?:html|txt|xml))["']""")
LOAD = re.compile(r"{%\s*load\s+(.+?)\s*%}")


def loader_name(parts: tuple[str, ...]) -> str:
    """A template's name as a loader finds it: its path below the last templates directory above it."""

    last = len(parts) - 1 - parts[::-1].index(TEMPLATES)
    return "/".join(parts[last + 1 :])


@functools.cache
def templates(root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(each template's name -> the templates that name it, each tag library -> the templates that load it),
    read once per tree."""

    naming: dict[str, set[str]] = {}
    loading: dict[str, set[str]] = {}
    try:
        listed = names("ls-files", "--cached", "--others", "--exclude-standard", "*.html", "*.txt", "*.xml", root=root)
    except RefError:
        # A tree outside git (a fixture's) holds no templates fast reads.
        listed = []
    for path in listed:
        if TEMPLATES not in Path(path).parts[:-1] or not (root / path).is_file():
            continue
        text = (root / path).read_text(encoding="utf-8")
        for name in TEMPLATE_NAME.findall(text):
            naming.setdefault(name, set()).add(path)
        for words in (load.split() for load in LOAD.findall(text)):
            for library in words[words.index("from") + 1 :] if "from" in words else words:
                loading.setdefault(library, set()).add(path)
    return naming, loading


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


def with_gone(reverse: dict[str, set[str]], deleted: list[str], files: list[Path]) -> dict[str, set[str]]:
    """``reverse`` with the modules that still import each deleted module, which the import graph of the tree
    without it cannot resolve: those that spell ``import <module>`` or ``from <module>``, or import it from
    its package by name."""

    gone = {module_name(Path(path)) for path in deleted if path.endswith(".py")} - reverse.keys()
    if not gone:
        return reverse
    spelled = {
        module: re.compile(
            rf"^\s*(?:from\s+{re.escape(module)}\s+import\b|import\s+{re.escape(module)}\b"
            rf"|from\s+{re.escape(module.rpartition('.')[0])}\s+import\s+[^\n]*\b{re.escape(module.rpartition('.')[2])}\b)",
            re.MULTILINE,
        )
        for module in gone
    }
    texts = {module_name(path): (chain.ROOT / path).read_text(encoding="utf-8") for path in files}
    return reverse | {module: {m for m, text in texts.items() if word.search(text)} for module, word in spelled.items()}


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
    """Those of ``modules`` the change reaches, and those that name an app whose models it reaches."""

    picked = {m for m in modules if m in reached}
    # A test that names an app's models exercises them: a model label
    # ("projects.Project") or the app label given to Django.
    words = [
        re.compile(
            rf"""["']{re.escape(app)}\.[A-Z]"""
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
