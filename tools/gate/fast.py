"""The inner-loop gate: only what the change can have broken.

Changed files against the merge base are mapped to the test modules that reach
them, by the import graph of the tree itself (nothing here lists an app), a
changed task definition to the tests that read the tasks, and a changed file
that is not Python to the tests that name its path. The
architecture tests always run, and a change to a module under hq/ or hq_sdk/
runs every test tagged tree, which finds its files by walking. Every other check is a mise task, run here by
name, so each exists once, and only when the change touches a path it reads
(TASKS). A change to what extensions render through
(templates, static, hq_sdk, the shared page and table layer) also runs the
extensions' own suites, composed, when HQ_LOCAL_PLUGINS names a set.
```mise run check``` stays the gate before a push.

Run as `mise run fast`; `mise run fast --help` says what it takes.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

from hq.platform.core.tests.task import Usage, usage
from tools.gate.verdict import Step, close, run
from tools.structural.classify import TEST

ROOT = Path(__file__).resolve().parents[2]
# The quick gates, each with the paths a change must touch for it to run, as
# fnmatch patterns, where `*` crosses directories. Those selected run side by
# side; each Django gate opens a database of its own (the django task
# template). A drift gate's paths are its sources and the file it derives.
PYTHON = ("*.py",)
TASKS: dict[str, tuple[str, ...]] = {
    "lint:ruff": PYTHON,
    "lint:mypy": PYTHON,
    # The graph fingerprints functions in every language it indexes.
    "lint:structural": (*PYTHON, "*.go", "*.js", "*.mjs", "*.sh", "mise-tasks/*", "tools/structural/*"),
    "lint:css": ("static/css/*", "tools/lint/*"),
    "lint:deptry": ("hq/*.py", "hq_sdk/*.py"),
    "lint:mise-tasks": ("mise-tasks/*",),
    "test:django": PYTHON,
    "test:migrations": ("hq/*.py",),
    "lint:drift-sdk-contract": (*PYTHON, "hq_sdk/*", "docs/Plugins.md"),
    "lint:drift-api-document": (*PYTHON, "hq/platform/api/hq-api.openapi.json"),
    "lint:drift-bridge-contract": (*PYTHON, "controller/api/*", "hq/config/controller-connections.json"),
    "lint:drift-connections-reference": (*PYTHON, "docs/Connections.md"),
    "lint:drift-font": ("tools/fonts/*", "static/fonts/*", "static/css/*"),
}
# Paths a gate never reads, so a change to them alone leaves it out: the
# structural bar drops test code before it pairs anything.
UNREAD = {"lint:structural": TEST}
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
# change to a module here runs every test tagged tree
# (hq/platform/application/tests/test_tree_tag.py holds the tag to the walks).
TREE = ("hq/*.py", "hq_sdk/*.py")
# The module every test that reads the tasks goes through; a change to a task
# definition reaches those tests as a change to it would.
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


def defines_tasks(path: str) -> bool:
    """A file mise reads this checkout's tasks from: mise.toml, or one in mise-tasks/, mise's default task directory."""

    return path == "mise.toml" or path.startswith("mise-tasks/")


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


def git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def merge_base(base: str | None) -> str:
    """The merge base with the named base, or with the first default that exists.

    A base that was asked for and does not resolve fails: falling back would
    quietly test a different change.
    """

    for candidate in (base,) if base else ("origin/main", "main"):
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except subprocess.CalledProcessError, IndexError:
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


def imports(path: Path, modules: set[str]) -> set[str]:
    """Modules of this tree a file imports, or a test names in a string (mock targets).

    A string outside a test is a late-bound reference, such as a domain's
    declaration naming its urlconf. Followed, the registry would reach every
    app and every change would select the whole suite.
    """

    try:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    reached = {module for name in named(tree, path) for module in reaches(name, modules)}
    reached.discard(module_name(path))
    return reached


def named(tree: ast.Module, path: Path) -> set[str]:
    """Every dotted name a file imports, and in a test every dotted string."""

    package = module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    test = is_test(path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            head = imported_from(node, package)
            names.add(head)
            names.update(f"{head}.{alias.name}" for alias in node.names)
        elif test and is_dotted_string(node):
            names.add(node.value)
    return names


def imported_from(node: ast.ImportFrom, package: list[str]) -> str:
    if not node.level:
        return node.module or ""
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


def is_dotted_string(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "." in node.value
        and " " not in node.value
        and len(node.value) < 200
    )


def reaches(name: str, modules: set[str]) -> set[str]:
    """The longest module a dotted name starts with, and the package __init__
    files above it, which importing it also runs."""

    pieces = name.split(".")
    for end in range(len(pieces), 0, -1):
        if ".".join(pieces[:end]) in modules:
            return {".".join(pieces[:n]) for n in range(1, end + 1)} & modules
    return set()


def importers(files: list[Path]) -> dict[str, set[str]]:
    """module -> modules that import it."""

    modules = {module_name(p) for p in files}
    reverse: dict[str, set[str]] = {m: set() for m in modules}
    for path in files:
        me = module_name(path)
        for target in imports(path, modules):
            reverse[target].add(me)
    return reverse


def django_apps() -> dict[str, str]:
    """Actual AppConfig source prefixes mapped to their stable Django labels."""

    apps: dict[str, str] = {}
    for path in python_files():
        if path.name != "apps.py" or path.parts[0] != "hq":
            continue
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            values = {
                target.id: assignment.value.value
                for assignment in node.body
                if isinstance(assignment, ast.Assign)
                for target in assignment.targets
                if isinstance(target, ast.Name)
                if isinstance(assignment.value, ast.Constant) and isinstance(assignment.value.value, str)
            }
            name = values.get("name")
            if name:
                apps[name.replace(".", "/")] = values.get("label", name.rsplit(".", 1)[-1])
    return apps


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
    modules = set(reverse)
    apps = django_apps()
    seen: set[str] = set()
    seeds: set[str] = set()
    touched: set[str] = set()
    for path in [*changed, *deleted]:
        if path.endswith(".py"):
            seeds.add(module_name(Path(path)))
        elif defines_tasks(path):
            seeds.add(TASK_READER)
        pkg = owner(path, apps)
        if pkg:
            touched.add(pkg)
            # One hop only: a test that imports the app. Following importers
            # further would reach every module through the shared layers.
            prefixes = [prefix.replace("/", ".") for prefix, label in apps.items() if label == pkg]
            members = {m for m in modules if any(m == prefix or m.startswith(prefix + ".") for prefix in prefixes)}
            seen |= members
            seen.update(i for m in members for i in reverse[m])
    reached = frontier = seeds
    for _ in range(REACH):
        frontier = {i for m in frontier for i in reverse.get(m, ())} - reached
        reached |= frontier
    return seen | reached, touched


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


def test_commands(labels: list[str], parallel: str, *, tree: bool) -> list[tuple[str, list[str]]]:
    """The test runs, each with its label: the selected modules, then with
    `tree` every test tagged tree, once, in a pass of its own.

    The label "." is the whole tree; named, the runner keeps the source tests
    a bare run leaves out.
    """

    test = [sys.executable, "manage.py", "test", "--noinput", "--parallel", parallel]
    if not tree:
        return [("tests", [*test, *labels])]
    return [("tests", [*test, "--exclude-tag", "tree", *labels]), ("tree tests", [*test, "--tag", "tree", "."])]


def step(label: str, cmd: list[str]) -> Step:
    print(f"[fast] {label}", flush=True)
    return run(label, cmd)


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

    tree = reaches_tree([*changed, *deleted])
    if tree:
        print("[fast] a module under hq/ or hq_sdk/ changed: every test tagged tree runs too")
    if list_only:
        print("\n".join(labels))
        return 0

    steps = []
    if tasks := selected_tasks([*changed, *deleted]):
        print(f"[fast] the quick gates that read what changed: {' '.join(tasks)}")
        steps.append(step("quick gates", tasks_command(tasks, parallel)))
    else:
        print("[fast] no quick gate reads what changed")
    steps.extend(step(label, cmd) for label, cmd in test_commands(labels, parallel, tree=tree))
    if os.environ.get("HQ_LOCAL_PLUGINS") and touches_shared_ui([*changed, *deleted]):
        # test:composed hands what follows to manage.py test.
        steps.append(
            step(
                "extension suites, composed (shared UI changed)",
                ["mise", "run", "test:composed", "--", "--installed-plugins", "--parallel", parallel],
            )
        )
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    close("fast", started, steps, " (full gate before push: `mise run check`)")
    return 0 if all(s.ok for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main(usage()))
