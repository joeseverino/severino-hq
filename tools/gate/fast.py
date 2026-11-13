"""The inner-loop gate: only what the change can have broken.

What it selects from the changed files, and which quick gates it runs beside
them: docs/Development.md, What `fast` runs. ``mise run check`` stays the gate
before a push.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from hq.platform.application.tests.reads import Reads
from hq.platform.application.tests.tree import TreeTests, tree_tests
from hq.platform.core.tests.task import RefError, Usage, changed_paths, git, names, usage
from tests.mise_tasks import ROOT as TASKS_ROOT, body, mise_tasks, run_files, runs
from tools.gate import testmap
from tools.gate.verdict import Gate, Step, run

ROOT = Path(__file__).resolve().parents[2]
# The tag of the quick gates: each declares the files it reads as its sources,
# and mise's --affected picks those a change touched. Those selected run side
# by side with the tests, in one mise run; each Django gate opens a database of
# its own (the django task template).
QUICK = "fast"
# The shell gate also reads every file its lister names, which no glob can, so
# a change selects it when the lister names a path it touched: as this tree has
# it, or as the base had it once deleted.
SHELL = "lint:shell"
SHELL_SOURCES = ROOT / "tools" / "gate" / "shell-sources.sh"
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
# Past this share of the suite the change is wide, and the whole suite runs:
# the rest costs little beside it, and only a whole run's pass starts the
# chain of recorded trees later runs read from (record_pass).
WIDE = 0.6
# What extensions render through. A change here reaches their pages, which no
# host test imports, so with an extension set named their suites run too.
SHARED_UI = (
    "templates/",
    "static/",
    "hq_sdk/",
    "hq/platform/application/tables.py",
    "hq/platform/application/pages.py",
    "hq/platform/application/ui.py",
    "hq/platform/application/moments.py",
)
# Said when such a change finds no extension set to run.
SKIPPED = (
    "shared UI changed, but mise.local.toml names no extension set, so the extensions' suites were skipped:"
    " `mise run setup:extensions <checkout>...` names one"
)


def defines_tasks(path: str) -> bool:
    """A file mise reads this checkout's tasks from (mise.toml, or one in
    mise-tasks/, mise's default task directory), or one a task's run names,
    which the tests that read the tasks read with it."""

    return path == "mise.toml" or path.startswith("mise-tasks/") or path in run_files()


def touches_shared_ui(paths: list[str]) -> bool:
    return any(path.startswith(SHARED_UI) or "/templatetags/" in path for path in paths)


def quick_gates() -> tuple[str, ...]:
    """Every task tagged QUICK."""

    return tuple(quick_tasks())


def quick_tasks() -> dict[str, dict[str, Any]]:
    """Every task tagged QUICK, by name, as mise lists it, read once per checkout."""

    return _quick_tasks(ROOT)


@functools.cache
def _quick_tasks(root: Path) -> dict[str, dict[str, Any]]:
    """A cache per checkout, so a run against a fixture (a test's) never answers for another."""

    listed = mise_json("tasks", "ls", "--tag", QUICK, "--hidden", "--local", "--json", root=root)
    return {task["name"]: task for task in listed}


# A quick gate whose tool fixes what it finds declares this flag, which writes
# the fixes instead of checking (mise-tasks/lint.toml); fast runs it first.
FIX = re.compile(r'^flag "--fix"', re.MULTILINE)
# A file task declares its usage in its own header, which mise's listing leaves out.
FILE_USAGE = re.compile(r"^#USAGE (.*)$", re.MULTILINE)


def fixers(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``tasks`` whose usage declares the fix flag."""

    quick = quick_tasks()
    return tuple(task for task in tasks if task in quick and FIX.search(usage_of(quick[task])))


def usage_of(task: dict[str, Any]) -> str:
    """A task's usage spec, from its listing or a file task's header."""

    if task.get("file"):
        return "\n".join(FILE_USAGE.findall(Path(task["file"]).read_text(encoding="utf-8")))
    return task.get("usage") or ""


def fixed_first(
    gate: Gate, tasks: tuple[str, ...], base: str, paths: tuple[list[str], list[str]]
) -> tuple[list[str], list[str]]:
    """The changed and deleted paths once the fix modes of ``tasks`` but the
    recordings' have run, and the gate set to judge the tree they left, saying
    what they rewrote."""

    fixed = fix(tuple(task for task in tasks if task not in recordings()))
    if not fixed:
        return paths
    print(f"[fast] the quick gates' fixes rewrote: {' '.join(fixed)}", flush=True)
    gate.restart()
    return changed_paths(base, ROOT)


def fix(tasks: tuple[str, ...]) -> list[str]:
    """Run the fix mode of each of ``tasks`` that has one, in one run of mise,
    and return the paths it rewrote, so the gates then judge fixed files and
    formatting never fails a run. What a fixer still finds, its gate reports."""

    fixing = fixers(tasks)
    if not fixing:
        return []
    before = dirty()
    words = [word for task in fixing for word in (":::", task, "--fix")][1:]
    subprocess.run(["mise", "run", "--continue-on-error", *words], cwd=ROOT, check=False, capture_output=True)
    after = dirty()
    # A path in one alone was rewritten too: one the fix left as HEAD has it is no longer dirty.
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


# A quick gate tagged this is a recording: it runs a set of test modules and
# compares what they receive with a file (lint:drift-api-examples). It reads
# Python only through those tests, so a change to Python alone selects it
# where it selects one of them. Its fix is the same run written over the file,
# so it runs only once the check has failed, and the gates and tests the
# rewritten files select then judge what it left.
RECORDING = "recording"
# A run of the test labels named after it, and a task a run starts.
TEST_RUN = re.compile(r"\bmanage\.py test((?:\s+[\w.]+)+)")
RUNS_TASK = re.compile(r"\bmise run ([\w:-]+)")


def recordings() -> tuple[str, ...]:
    """Every quick gate tagged RECORDING."""

    return tuple(name for name, task in quick_tasks().items() if RECORDING in (task.get("tags") or ()))


def recorded_labels(name: str) -> list[str]:
    """The test labels a recording runs, in its own run or one a task its run starts."""

    tasks = mise_tasks()
    own = body(tasks[name])
    scripts = [own, *(body(tasks[started]) for started in RUNS_TASK.findall(own) if started in tasks)]
    return [label for script in scripts for run in TEST_RUN.findall(script) for label in run.split()]


def unreached_recordings(tasks: tuple[str, ...], paths: list[str], labels: list[str]) -> list[str]:
    """The recordings among ``tasks`` that a change to Python files alone
    (``paths``) selected, though the tests it selects (``labels``) hold none
    of those they record. A recording that names no test is always kept."""

    if not paths or not all(path.endswith(".py") for path in paths):
        return []
    return [
        name
        for name in tasks
        if name in recordings()
        and (recorded := recorded_labels(name))
        and not any(label == held or label.startswith(f"{held}.") for label in labels for held in recorded)
    ]


def fixed_after(gate: Gate, steps: list[Step], files: list[Path], parallel: str) -> list[Step]:
    """``steps``, and when a recording failed in the first, its fix and the
    run again of the gates and tests the files it rewrote select, the fixed
    recordings among them. The first step's failure stands unless it failed
    on the fixed recordings alone, and the run judges the tree the fix left."""

    first = steps[0]
    failed = [task for task in first.tasks if task in recordings()]
    fixed = fix(tuple(failed)) if failed else []
    if not fixed:
        return steps
    print(f"[fast] {' '.join(failed)} failed, so its fix ran and rewrote: {' '.join(fixed)}", flush=True)
    gate.restart()
    # HEAD as the base: what the fix rewrote is in the checkout, beside any work not yet committed.
    tasks = (*failed, *(task for task in selected_tasks(fixed, "HEAD") if task not in failed))
    labels = with_tree(test_labels(files, *affected(fixed, [], files))[0], fixed)
    if set(first.tasks) <= set(failed) and not first.tests and not first.unimported:
        first = replace(first, ok=True, tasks=[])
    return [first, *gates_and_tests(tasks, labels, files, parallel, label="the gates and tests again, on the fix")]


def dirty() -> dict[str, str]:
    """Each path where the checkout differs from HEAD, untracked ones included, by a digest of its bytes."""

    present, _ = changed_paths("HEAD", ROOT)
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in present if (ROOT / path).is_file()}


def selected_tasks(paths: list[str], base: str) -> tuple[str, ...]:
    """The quick gates a change can have broken: those whose sources or definition it touches, as mise's
    --affected reads the change from ``base`` with this checkout's work, and the shell gate when its lister
    names one of ``paths``. HEAD is named so no CI metadata stands in for it."""

    affected = ("--affected", "--affected-base", base, "--affected-head", "HEAD", "--affected-json")
    selected = tuple(task["name"] for task in mise_json("run", "--tag", QUICK, *affected)["tasks"])
    return selected if SHELL in selected or not shell_files(paths, base) else (*selected, SHELL)


def mise_json(*args: str, root: Path | None = None) -> Any:
    """What a mise command prints as JSON, run in ``root`` or this checkout; its errors print as they come."""

    run = subprocess.run(["mise", *args], cwd=root or ROOT, check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(run.stdout)


def shell_files(paths: list[str], base: str | None = None) -> list[str]:
    """The paths the shell gate's lister names: each as this tree has it, or as ``base`` had it."""

    here = [path for path in paths if (ROOT / path).is_file()]
    found = list_shell(ROOT, here) if here else []
    gone = [path for path in paths if path not in here]
    if base is None or not gone:
        return found
    with tempfile.TemporaryDirectory() as folder:
        for path in gone:
            # A path the base lacks too is no shell file of either.
            held = subprocess.run(
                ["git", "cat-file", "blob", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=False
            )
            if held.returncode == 0:
                (Path(folder) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(folder) / path).write_bytes(held.stdout)
        return found + list_shell(Path(folder), gone)


def list_shell(root: Path, paths: list[str]) -> list[str]:
    """Those of ``paths``, under ``root``, that the shell gate's lister names."""

    listed = subprocess.run(
        ["/bin/sh", str(SHELL_SOURCES), *paths], cwd=root, check=True, capture_output=True, text=True
    )
    return listed.stdout.splitlines()


# How far back first-parent history is read for a commit whose tree passed.
PASSED_REACH = 300


def fast_base(asked: str | None) -> tuple[str, str]:
    """(the commit the change is read from, why it is the one).

    With none asked for, the nearest commit whose tree passed: what the change
    from it leaves unselected had passed, which is what lets ``record_pass``
    record this tree. Without one in reach, the merge base with the branch
    HEAD lands on."""

    if asked:
        return merge_base(asked), f"the merge base with {asked}"
    if found := passed_ancestor(ROOT):
        commit, back = found
        return commit, f"the nearest commit whose tree passed, {back} back on first-parent history"
    reason = f"no commit within {PASSED_REACH} back has a passed tree, so the merge base with the branch HEAD lands on"
    return merge_base(None), reason


def passed_ancestor(root: Path) -> tuple[str, int] | None:
    """(the nearest commit on HEAD's first-parent history whose tree passed,
    how many commits back), or None within PASSED_REACH."""

    return recorded_ancestor(passes(root), "", root)


def recorded_ancestor(folder: Path, suffix: str, root: Path) -> tuple[str, int] | None:
    """(the nearest commit on HEAD's first-parent history whose tree has a
    file named by its hash and ``suffix`` in ``folder``, how many commits
    back), or None within PASSED_REACH."""

    if not folder.is_dir():
        return None
    history = answered("log", "--first-parent", f"--max-count={PASSED_REACH}", "--format=%H %T", "HEAD", root=root)
    for back, line in enumerate(history):
        commit, _, tree = line.partition(" ")
        if (folder / f"{tree}{suffix}").is_file():
            return commit, back
    return None


def merge_base(base: str | None) -> str:
    """The merge base with the named base, or else with the branch HEAD lands
    on, or else with the first default that exists.

    A base that was asked for and does not resolve fails: falling back would
    quietly test a different change.
    """

    for candidate in (base,) if base else (*landing(), "origin/main", "main"):
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except RefError, IndexError:
            if base:
                sys.exit(f"fast: {base} has no merge base with HEAD; pass a ref that exists")
    sys.exit("fast: no base found; pass one (mise run fast <ref>)")


def landing(root: Path = ROOT) -> list[str]:
    """The branch HEAD lands on: its own branch's upstream, or else the local
    branch tracking a remote one whose history HEAD left most recently. A
    worktree's branch tracks nothing and is made from the branch it lands on,
    which may be far ahead of main, so main would select most of the suite."""

    if upstream := answered("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", root=root):
        return upstream
    current = git("branch", "--show-current", root=root)
    own = f"refs/heads/{current[0]}" if current else ""
    listed = git("for-each-ref", "--format=%(refname) %(upstream)", "refs/heads", root=root)
    tracking = [ref for ref, _, upstream in (line.partition(" ") for line in listed) if upstream and ref != own]
    left = {
        ref: int(git("rev-list", "--count", f"{fork[0]}..HEAD", root=root)[0])
        for ref in tracking
        if (fork := answered("merge-base", "HEAD", ref, root=root))
    }
    return sorted(left, key=left.__getitem__)[:1]


def answered(*args: str, root: Path = ROOT) -> list[str]:
    """What a git command prints, or nothing where git refuses (no upstream, no history in common)."""

    try:
        return git(*args, root=root)
    except RefError:
        return []


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def python_files() -> list[Path]:
    found = set(names("ls-files", "--cached", "--others", "--exclude-standard", "*.py", root=ROOT))
    return sorted(Path(f) for f in found if (ROOT / f).exists() and not SKIP_PARTS & set(Path(f).parts))


def is_test(path: Path) -> bool:
    """A test module the suite discovers: every parent directory is a package.

    A test outside one (the structural bar's) belongs to its own gate.
    """

    return (
        path.suffix == ".py"
        and path.name != "__init__.py"
        and path.name.startswith("test")
        and all((ROOT / parent / "__init__.py").exists() for parent in path.parents if parent != Path())
    )


def packages(module: str, modules: set[str]) -> set[str]:
    """The module and the package __init__ files above it, which importing it also runs."""

    pieces = module.split(".")
    return {".".join(pieces[:n]) for n in range(1, len(pieces) + 1)} & modules


def importers(files: list[Path]) -> dict[str, set[str]]:
    """module -> modules that import it, read once per tree and file list, so
    the tests that map many changes read the tree once."""

    return _importers(ROOT, tuple(files))


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
        cwd=ROOT,
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
            apps.update(app_configs(ast.parse((ROOT / path).read_text(encoding="utf-8"))))
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

    index = Naming(named(ROOT, tuple(files)))
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

    found = recorded_ancestor(maps(ROOT), ".json", ROOT)
    if not found:
        return labels
    commit, back = found
    tree = git("rev-parse", f"{commit}^{{tree}}", root=ROOT)[0]
    the_map = testmap.load(maps(ROOT) / f"{tree}.json")
    changed, deleted = changed_paths(commit, ROOT)
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
    before = subprocess.run(["git", "cat-file", "blob", f"{commit}:{path}"], cwd=ROOT, capture_output=True, check=False)
    if before.returncode:
        return False
    return testmap.bodies_only(before.stdout.decode(), (ROOT / path).read_text(encoding="utf-8"))


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
        if name not in picked and any(word.search((ROOT / path).read_text(encoding="utf-8")) for word in words):
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
    found = walking.result() if walking else tree_tests(ROOT)
    names = paths if listed is None else listed
    reached = [label for label, held in found if held is None or Reads(*held).selects(paths, names)]
    print(f"[fast] {len(reached)} of the {len(found)} tests tagged tree run: those the change reaches")
    return [*labels, *reached]


def listed_change(base: str, deleted: list[str]) -> list[str]:
    """The paths a change adds or deletes since ``base``, untracked ones included: all a listing can tell."""

    added = names("diff", "--name-only", "--no-renames", "--diff-filter=A", base, root=ROOT)
    return sorted({*added, *names("ls-files", "--others", "--exclude-standard", root=ROOT), *deleted})


def walk_for_tree() -> Future[TreeTests]:
    """The walk for the tests tagged tree, started now on a thread: it runs in a
    process of its own (``apart``), so it overlaps the reading of the import
    graph and the paths the modules name, which it does not depend on."""

    pool = ThreadPoolExecutor(max_workers=1)
    walking = pool.submit(tree_tests, ROOT)
    # Leaving the pool's block would wait for the walk; this lets it run on.
    pool.shutdown(wait=False)
    return walking


# The module every test that reads each of the browser gate's modules goes
# through. Such a test is a source check that starts no browser (the probe
# check among them), so those selected run ahead of the rest, in fast:source,
# and a refusal there is printed before the suite starts.
BROWSER_MODULES = "hq.platform.core.tests.browser_modules"
# fast's test run, the task that runs it after fast:source
# (mise-tasks/fast/), and the config environment of the one run of mise it shares
# with the quick gates (mise.fast.toml), whose output is mise's summary.
TESTS = "fast:tests"
ENVIRONMENT = "fast"
# The variable fast:tests reads the labels of fast:source from, its dependency.
SOURCE = "HQ_FAST_SOURCE"


def source_first(labels: list[str], files: list[Path]) -> tuple[list[str], list[str]]:
    """(the labels of the tests that read each browser module, the rest)."""

    readers = importers(files).get(BROWSER_MODULES, set())
    first = [label for label in labels if any(label == m or label.startswith(m + ".") for m in readers)]
    return first, [label for label in labels if label not in first]


def run_command(
    tasks: tuple[str, ...], source: list[str], labels: list[str], parallel: str, *, record: bool = False
) -> list[str]:
    """One run of mise for the quick gates and the tests, side by side, which
    runs every task to its end when one fails, so one run names every failure
    it reaches. A gate that takes the heavy lock itself (the shell gate) runs in
    it too, holding the run open until it has had its turn. A test both
    selected and tagged tree runs once, since Django's runner drops a repeated
    test. A recording run (``record``) measures the tests for the map of the tests
    that ran each file (``record_map``).

    A task's own output setting outranks the config's but not ``--output``, so
    the summary is the environment's: the tests' task prints as it runs, its
    wait for the heavy turn included, beside each gate's line as it ends."""

    jobs = ["--jobs", "1"] if parallel == "1" else []
    gates = [word for task in tasks for word in (task, ":::")]
    tests = [TESTS, "--parallel", parallel, *(["--map", str(ROOT / testmap.DATA)] if record else []), *labels]
    return [
        "env",
        f"{SOURCE}={' '.join(source)}",
        "mise",
        "--env",
        ENVIRONMENT,
        "run",
        "--continue-on-error",
        *jobs,
        *gates,
        *tests,
    ]


def gates_and_tests(
    tasks: tuple[str, ...],
    labels: list[str],
    files: list[Path],
    parallel: str,
    *,
    record: bool = False,
    label: str = "quick gates and tests",
) -> list[Step]:
    """The one run of the quick gates and the tests."""

    if tasks:
        print(f"[fast] the quick gates that read what changed: {' '.join(tasks)}")
    else:
        print("[fast] no quick gate reads what changed")
    source, rest = source_first(labels, files)
    if source:
        print(f"[fast] first, the source checks that read the browser gate's modules: {' '.join(source)}")
    return [step(label, run_command(tasks, source, rest, parallel, record=record))]


def step(label: str, cmd: list[str]) -> Step:
    print(f"[fast] {label}", flush=True)
    return run(label, cmd)


def extension_suites(parallel: str) -> list[Step]:
    """The extensions' suites, composed, when the shared UI changed: run with a set named, else said skipped."""

    if not os.environ.get("HQ_LOCAL_PLUGINS"):
        print(f"[fast] {SKIPPED}", flush=True)
        return []
    # test:composed hands what follows to manage.py test.
    command = ["mise", "run", "test:composed", "--", "--installed-plugins", "--parallel", parallel]
    return [step("extension suites, composed (shared UI changed)", command)]


# Where a passed tree is recorded, under the repository's common git directory
# so every worktree shares it: an empty file named by the tree's hash, which a
# commit keeps when it is re-signed. The agent hooks read it to hold a brief's
# base to a pass.
PASSES = ("severino", "fast-passes")


def passes(root: Path = ROOT) -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", root=root)[0]
    return Path(common).joinpath(*PASSES)


def maps(root: Path = ROOT) -> Path:
    """Where the maps of the tests that ran each file are kept, beside the passed trees."""

    return passes(root).parent / testmap.MAPS


def tree_of(commit: str) -> str:
    return git("rev-parse", f"{commit}^{{tree}}")[0]


def record_pass(tree: str | None, base: str, *, whole: bool) -> None:
    """Record ``tree``, the one a passing run judged (None when the checkout
    changed in the run), and say so, when the run proves it: it selected the
    whole suite or the base's tree passed too, so what the change left
    unselected is what had passed. A run that selected only what changed from
    an unpassed base proves nothing about the rest."""

    if not tree:
        return
    folder = passes()
    if not whole and not (folder / tree_of(base)).is_file():
        said = "not recorded as passed: its base's tree has no pass, and the run was not the whole suite"
    else:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / tree).touch()
        said = "recorded as passed"
    print(f"[fast] tree {tree[:9]} {said}", flush=True)


def record_map(tree: str | None) -> None:
    """Keep the map a whole run measured as ``tree``'s, when the run passed on
    a tree that held still, and say what it holds. The data goes once read:
    the map holds what it says."""

    data = ROOT / testmap.DATA
    if not tree or not any(data.parent.glob(f"{data.name}.*")):
        return
    found = testmap.read(data, ROOT)
    data.unlink(missing_ok=True)
    testmap.save(maps(), tree, found)
    held = f"{len(found.files)} files, {len(found.ran)} test modules"
    print(f"[fast] the map of the tests that ran each file is kept for tree {tree[:9]}: {held}", flush=True)


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


def judge(gate: Gate, steps: list[Step], base: str, *, whole: bool) -> int:
    """Close the run on ``steps``: a pass records its tree, and a whole run's its map too."""

    passed = all(s.ok for s in steps)
    if passed:
        judged = gate.judged()
        record_pass(judged, base, whole=whole)
        if whole:
            record_map(judged)
    gate.close(steps, " (full gate before push: `mise run check`)")
    return 0 if passed else 1


def main(asked: Usage) -> int:
    # The task's usage reads CHECK_BASE and CHECK_PARALLEL behind its own arguments.
    parallel = asked.text("parallel", "auto")
    list_only = asked.switch("list")
    whole_asked = asked.switch("whole")
    # A run opens on where its verdict is kept; a listing keeps none.
    gate = None if list_only else Gate("fast")
    base, why = fast_base(asked.text("base") or None)
    changed, deleted = changed_paths(base, ROOT)
    selected = selected_tasks([*changed, *deleted], base) if gate else ()
    if gate:
        changed, deleted = fixed_first(gate, selected, base, (changed, deleted))
    walking = walk_for_tree() if changed or deleted else None
    files = python_files()
    labels, total, (reached, touched) = selection(changed, deleted, files)
    counts = f"{len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules"
    print(f"[fast] base {base[:9]}, {why}: {counts}")
    every_test = len(labels) == total > 0
    if gate and not every_test and (whole_asked or len(labels) > WIDE * total):
        said = "asked for" if whole_asked else "wide change"
        print(f"[fast] {said}: the whole suite and every quick gate run, so a pass records this tree and its map")
        labels, every_test = whole_suite(files), True

    paths = [*changed, *deleted]
    # Read before the tests tagged tree join: their selection says nothing of the change's reach.
    unreached = unreached_recordings(selected, paths, labels) if gate and not every_test else []
    labels = with_tree(labels, paths, listed_change(base, deleted) if paths else [], walking)
    if gate is None:
        print("\n".join(labels))
        return 0

    if unreached:
        print(
            f"[fast] not run, as the change is Python alone and selects none of the tests each records: {' '.join(unreached)}"
        )
    whole = (*quick_gates(), *group_tests().values())
    kept = tuple(task for task in selected if task not in unreached)
    tasks = whole if every_test else (*kept, *group_tasks(files, reached, touched))
    whole_run = every_test and set(tasks) == set(whole)
    steps = fixed_after(gate, gates_and_tests(tasks, labels, files, parallel, record=whole_run), files, parallel)
    if touches_shared_ui([*changed, *deleted]):
        steps.extend(extension_suites(parallel))
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    return judge(gate, steps, base, whole=whole_run)


if __name__ == "__main__":
    sys.exit(main(usage()))
