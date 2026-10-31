"""The inner-loop gate: only what the change can have broken.

What it selects from the changed files, and which quick gates it runs beside
them: docs/Development.md, What `fast` runs. ``mise run check`` stays the gate
before a push.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

from hq.platform.application.tests.tree import tagged
from hq.platform.core.tests.task import RefError, Usage, git, usage
from tests.mise_tasks import run_files
from tools.gate.verdict import Step, close, run
from tools.structural.classify import TEST

ROOT = Path(__file__).resolve().parents[2]
# Only the test run waits for the machine's heavy turn: its parallel workers
# hold a suite's memory for most of fast's time, while each quick gate is one
# short process, and the composed suites take the turn as their own task
# decides.
HEAVY = ROOT / "tools" / "lib" / "heavy.sh"
# The quick gates, each with the paths a change must touch for it to run, as
# fnmatch patterns, where `*` crosses directories. Those selected run side by
# side; each Django gate opens a database of its own (the django task
# template). A drift gate's paths are its sources and the file it derives.
PYTHON = ("*.py",)
TASKS: dict[str, tuple[str, ...]] = {
    "lint:ruff": PYTHON,
    "lint:mypy": PYTHON,
    "lint:ast-grep": (*PYTHON, "tools/lint/ast-grep/*"),
    # The graph fingerprints functions in every language it indexes.
    "lint:structural": (*PYTHON, "*.go", "*.js", "*.mjs", "*.sh", "mise-tasks/*", "tools/structural/*"),
    "lint:complexity": (*PYTHON, "complexipy.toml"),
    "lint:controller": ("controller/*",),
    "lint:css": ("static/css/*", "tools/lint/*.mjs", "tools/lint/package*.json"),
    "lint:js": ("static/js/*", "tools/lint/*.mjs", "tools/lint/package*.json"),
    "lint:deptry": ("hq/*.py", "hq_sdk/*.py"),
    "lint:imports": ("hq/*.py",),
    "lint:prose": (*PYTHON, "*.html", "*.md", "*.js", "*.css", "*.go", "*.sh", "tools/lint/vale/*"),
    "lint:mise-tasks": ("mise-tasks/*",),
    "test:migrations": ("hq/*.py",),
    "lint:drift-sdk-contract": (*PYTHON, "hq_sdk/*", "docs/Plugins.md"),
    "lint:drift-api-document": (*PYTHON, "hq/platform/api/hq-api.openapi.json"),
    "lint:drift-bridge-contract": (*PYTHON, "controller/api/*", "hq/config/controller-connections.json"),
    "lint:drift-connections-reference": (*PYTHON, "docs/Connections.md"),
    "lint:drift-font": ("tools/fonts/*", "static/fonts/*", "static/css/*"),
}
# Paths a gate never reads, so a change to them alone leaves it out: the
# structural bar drops test code before it pairs anything, and complexipy.toml
# leaves it out.
UNREAD = {"lint:structural": TEST, "lint:complexity": TEST}
# What every gate reads: the dependencies, the tool pins and the task
# definitions. A change to one runs them all.
EVERY_TASK = (
    "pyproject.toml",
    "uv.lock",
    "mise.toml",
    "mise.lock",
    "mise-tasks/*.toml",
    "tools/pipeline/toolchain.env",
)
# Never part of the Django suite: other languages and vendored trees.
SKIP_PARTS = {"node_modules", "controller", ".venv", "staticfiles"}
ALWAYS = ("architecture", "new_domain")
# A test that walks a directory finds a module there through no import, so a
# change to a module here runs every test tagged tree, in the same run as the
# rest (hq/platform/application/tests/test_tree_tag.py holds the tag to the walks).
TREE = ("hq/*.py", "hq_sdk/*.py")
# The module every test that reads the tasks goes through; a change to a task
# definition, or to a file a task runs, reaches those tests as a change to it would.
TASK_READER = "tests.mise_tasks"
# How many importers away from a changed module a test is still selected: one
# that imports it, or imports a module that does. Further out the walk reaches
# most of the tree through the shared layers, and the module in between has
# tests of its own at this distance.
REACH = 2
# Past this share of the suite the change is wide and the fast gate says so.
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


def reaches_tree(paths: list[str]) -> bool:
    return any(fnmatch(path, pattern) for path in paths for pattern in TREE)


def selected_tasks(paths: list[str]) -> tuple[str, ...]:
    """The quick gates a change can have broken: those whose paths it touches, or all when it touches EVERY_TASK."""

    def touched(patterns: tuple[str, ...], task: str = "") -> bool:
        unread = UNREAD.get(task)
        read = [path for path in paths if not (unread and unread.search(path))]
        return any(fnmatch(path, pattern) for path in read for pattern in patterns)

    if touched(EVERY_TASK):
        return tuple(TASKS)
    return tuple(task for task, patterns in TASKS.items() if touched(patterns, task))


def tasks_command(tasks: tuple[str, ...], parallel: str) -> list[str]:
    """One run of every task, side by side unless parallelism is ruled out.

    `-c` names every task that failed, and the configured keep-order output
    prints each one whole under its own name.
    """

    jobs = ["--jobs", "1"] if parallel == "1" else []
    separated = [word for task in tasks for word in (":::", task)][1:]
    return ["mise", "run", "-c", *jobs, *separated]


def merge_base(base: str | None) -> str:
    """The merge base with the named base, or with the first default that exists.

    A base that was asked for and does not resolve fails: falling back would
    quietly test a different change.
    """

    for candidate in (base,) if base else ("origin/main", "main"):
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except RefError, IndexError:
            if base:
                sys.exit(f"fast: {base} has no merge base with HEAD; pass a ref that exists")
    sys.exit("fast: no base found; pass one (mise run fast <ref>)")


def changed_files(base: str) -> tuple[list[str], list[str]]:
    """(present, deleted) paths: committed since the base, staged, unstaged, new."""

    present = set(git("diff", "--name-only", "--diff-filter=ACMRT", base))
    present |= set(git("ls-files", "--others", "--exclude-standard"))
    deleted = set(git("diff", "--name-only", "--diff-filter=D", base))
    return sorted(present), sorted(deleted)


def module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def python_files() -> list[Path]:
    found = set(git("ls-files", "--cached", "--others", "--exclude-standard", "*.py"))
    return sorted(Path(f) for f in found if (ROOT / f).exists() and not SKIP_PARTS & set(Path(f).parts))


def is_test(path: Path) -> bool:
    """A test module the suite discovers: every parent directory is a package.

    A test outside one (the structural bar's) belongs to its own gate.
    """

    return (
        path.suffix == ".py"
        and path.name != "__init__.py"
        and (path.name.startswith("test") or "fuzz" in path.parts)
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
def _importers(root: Path, files: tuple[Path, ...]) -> dict[str, set[str]]:  # noqa: ARG001 - root is a key of the cache
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


def django_apps() -> dict[str, str]:
    """Actual AppConfig source prefixes mapped to their stable Django labels."""

    apps: dict[str, str] = {}
    for path in python_files():
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


def affected(changed: list[str], deleted: list[str], files: list[Path]) -> tuple[set[str], set[str]]:
    """(modules reachable from the change through importers, apps it touches).

    A change in an app also selects the app's own tests and every test that
    imports any of the app: a test that drives its views through the client
    imports its models, never its views.
    """

    reverse = importers(files)
    apps = django_apps()
    paths = [*changed, *deleted]
    touched = {label for path in paths if (label := owner(path, apps))}
    return app_reach(touched, apps, reverse) | import_reach(seeds(paths), reverse), touched


def seeds(paths: list[str]) -> set[str]:
    """The modules a change starts from: each Python module it touched, and
    the module that reads the tasks when it touched a task's definition."""

    found = {module_name(Path(path)) for path in paths if path.endswith(".py")}
    return (found | {TASK_READER}) if any(defines_tasks(path) for path in paths) else found


def app_reach(touched: set[str], apps: dict[str, str], reverse: dict[str, set[str]]) -> set[str]:
    """Every module of the touched apps, and each module that imports one.

    One hop only: a test that imports the app. Following importers further
    would reach every module through the shared layers.
    """

    prefixes = [prefix.replace("/", ".") for prefix, label in apps.items() if label in touched]
    members = {m for m in reverse if any(m == prefix or m.startswith(prefix + ".") for prefix in prefixes)}
    return members | {i for m in members for i in reverse[m]}


def import_reach(start: set[str], reverse: dict[str, set[str]]) -> set[str]:
    """``start`` and the modules that import it, ``REACH`` hops out."""

    reached = frontier = set(start)
    for _ in range(REACH):
        frontier = {i for m in frontier for i in reverse.get(m, ())} - reached
        reached |= frontier
    return reached


def path_references(path: str) -> list[re.Pattern[str]]:
    """How a test names a file it reads: the path, or its directory when that
    is more than a top-level one, as one string ("a/b.yml") or as segments
    (`root / "a" / "b.yml"`, `Path("a", "b.yml")`)."""

    parts = Path(path).parts
    named = [parts, parts[:-1]] if len(parts) > 2 else [parts]
    between = r"""(?:/|["']\s*[/,]\s*["'])"""
    return [re.compile(rf"""["']{between.join(map(re.escape, p))}(?![\w.-])""") for p in named]


def test_labels(
    files: list[Path], reached: set[str], touched: set[str], read: list[str] | tuple[str, ...] = ()
) -> tuple[list[str], int]:
    """The test modules to run: those the change reaches by import, those that
    name an app it touched, those that name a file it changed that is not
    Python (`read`), and the architecture tests."""

    by_module = {module_name(p): p for p in files if is_test(p)}
    tests = set(by_module)
    chosen = {t for t in tests if t in reached}
    # A test that names an app exercises it: a URL namespace ("projects:list"),
    # a model label ("projects.Project") or the app label given to Django.
    words = [
        re.compile(
            rf"""["']{re.escape(app)}(?::|\.[A-Z])"""
            rf"""|(?:app_label\W+|get_model\(\s*|get_app_config\(\s*)["']{re.escape(app)}["']"""
        )
        for app in touched
    ]
    words += [pattern for path in read for pattern in path_references(path)]
    for name, path in by_module.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        if any(word.search(text) for word in words):
            chosen.add(name)
    chosen |= {t for t in tests if any(a in t for a in ALWAYS)}
    return sorted(chosen), len(tests)


def with_tree(labels: list[str], paths: list[str]) -> list[str]:
    """The selected labels, and every test tagged tree when a module under hq/ or hq_sdk/ changed."""

    return [*labels, *tagged(ROOT, "tree")] if reaches_tree(paths) else labels


def test_command(labels: list[str], parallel: str) -> list[str]:
    """One run of the selected tests, in the machine's heavy turn: a test both
    selected and tagged tree runs once, since Django's runner drops a
    repeated test."""

    return [str(HEAVY), sys.executable, "manage.py", "test", "--noinput", "--parallel", parallel, *labels]


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


def main(asked: Usage) -> int:
    started = time.monotonic()
    # The task's usage reads CHECK_BASE and CHECK_PARALLEL behind its own arguments.
    parallel = asked.text("parallel", "auto")
    list_only = asked.switch("list")
    base = merge_base(asked.text("base") or None)
    changed, deleted = changed_files(base)
    files = python_files()
    reached, touched = affected(changed, deleted, files)
    read = [path for path in [*changed, *deleted] if not path.endswith(".py")]
    labels, total = test_labels(files, reached, touched, read)
    print(f"[fast] base {base[:9]}: {len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules")
    if total and len(labels) / total > WIDE:
        print("[fast] wide change: most of the suite is reached; `mise run check` is the honest gate")

    if reaches_tree([*changed, *deleted]):
        print("[fast] a module under hq/ or hq_sdk/ changed: every test tagged tree runs too")
    labels = with_tree(labels, [*changed, *deleted])
    if list_only:
        print("\n".join(labels))
        return 0

    steps = []
    if tasks := selected_tasks([*changed, *deleted]):
        print(f"[fast] the quick gates that read what changed: {' '.join(tasks)}")
        steps.append(step("quick gates", tasks_command(tasks, parallel)))
    else:
        print("[fast] no quick gate reads what changed")
    steps.append(step("tests", test_command(labels, parallel)))
    if touches_shared_ui([*changed, *deleted]):
        steps.extend(extension_suites(parallel))
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    close("fast", started, steps, " (full gate before push: `mise run check`)")
    return 0 if all(s.ok for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main(usage()))
