"""The inner-loop gate: only what the change can have broken.

Changed files against the merge base are mapped to the test modules that reach
them, by the import graph of the tree itself (nothing here lists an app). The
architecture tests always run. Every other check is a mise task, run here by
name, so each exists once. A change to what extensions render through
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The gates that run whatever changed, side by side: each is quick, and each
# Django gate opens a database of its own (the django task template).
TASKS = ("checks:ruff", "checks:mypy", "tests:django", "tests:migrations", "drift:*")
# Never part of the Django suite: other languages, vendored trees, the browser
# pass that `mise run check` also leaves to `mise run browser`.
SKIP_PARTS = {"node_modules", "controller", "browser_tests", ".venv", "staticfiles"}
ALWAYS = ("architecture", "new_domain")
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


def touches_shared_ui(paths: list[str]) -> bool:
    return any(path.startswith(SHARED_UI) or "/templatetags/" in path for path in paths)


def tasks_command(tasks: tuple[str, ...], parallel: str) -> list[str]:
    """One mise run of every task, side by side unless parallelism is ruled out.

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

    asked = base or os.environ.get("CHECK_BASE")
    for candidate in (asked,) if asked else ("origin/main", "main"):
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except (subprocess.CalledProcessError, IndexError):
            if asked:
                sys.exit(f"fast: {asked} has no merge base with HEAD; pass a ref that exists")
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
        and (path.name.startswith("test") or ("fuzz" in path.parts and not is_fuzz_harness(path)))
        and all((ROOT / parent / "__init__.py").exists() for parent in path.parents if parent != Path())
    )


def is_fuzz_harness(path: Path) -> bool:
    """An atheris harness: run by its fuzzer, and in the suite through the test that drives it."""

    try:
        return "import atheris" in (ROOT / path).read_text()
    except OSError:
        return False


def imports(path: Path, modules: set[str]) -> set[str]:
    """Modules of this tree a file imports, or a test names in a string (mock targets).

    A string outside a test is a late-bound reference, such as a domain's
    declaration naming its urlconf. Followed, the registry would reach every
    app and every change would select the whole suite.
    """

    package = module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    try:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    test = is_test(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                head = ".".join([*base, *([node.module] if node.module else [])])
            else:
                head = node.module or ""
            names.add(head)
            names.update(f"{head}.{alias.name}" for alias in node.names)
        elif (
            test
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "." in node.value
            and " " not in node.value
            and len(node.value) < 200
        ):
            names.add(node.value)
    reached: set[str] = set()
    for name in names:
        pieces = name.split(".")
        # A dotted name reaches the longest module it starts with, and the
        # package __init__ files above it, which importing it also runs.
        for end in range(len(pieces), 0, -1):
            head = ".".join(pieces[:end])
            if head in modules:
                reached.add(head)
                reached.update(".".join(pieces[:n]) for n in range(1, end) if ".".join(pieces[:n]) in modules)
                break
    reached.discard(module_name(path))
    return reached


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


def test_labels(files: list[Path], reached: set[str], touched: set[str]) -> tuple[list[str], int]:
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
    for name, path in by_module.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        if any(word.search(text) for word in words):
            chosen.add(name)
    chosen |= {t for t in tests if any(a in t for a in ALWAYS)}
    return sorted(chosen), len(tests)


def step(label: str, cmd: list[str]) -> bool:
    print(f"[fast] {label}", flush=True)
    started = time.monotonic()
    code = subprocess.run(cmd, cwd=ROOT).returncode
    print(f"[fast] {label}: {time.monotonic() - started:.1f}s", flush=True)
    return code == 0


def main(argv: list[str]) -> int:
    started = time.monotonic()
    parallel = os.environ.get("CHECK_PARALLEL", "auto")
    list_only = "--list" in argv
    args = [a for a in argv if a != "--list"]
    base = merge_base(args[0] if args else None)
    changed, deleted = changed_files(base)
    files = python_files()
    reached, touched = affected(changed, deleted, files)
    labels, total = test_labels(files, reached, touched)
    print(f"[fast] base {base[:9]}: {len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules")
    if total and len(labels) / total > WIDE:
        print("[fast] wide change: most of the suite is reached; `mise run check` is the honest gate")

    if list_only:
        print("\n".join(labels))
        return 0

    ok = step(" ".join(TASKS), tasks_command(TASKS, parallel))
    ok &= step("tests", [sys.executable, "manage.py", "test", "--noinput", "--parallel", parallel, *labels])
    if os.environ.get("HQ_LOCAL_PLUGINS") and touches_shared_ui([*changed, *deleted]):
        # suite:composed hands what follows to manage.py test.
        ok &= step(
            "extension suites, composed (shared UI changed)",
            ["mise", "run", "suite:composed", "--", "--installed-plugins", "--parallel", parallel],
        )
    ok &= step("patch integrity", ["git", "diff", "--check", base])
    print(
        f"[fast] {'passed' if ok else 'FAILED'} in {time.monotonic() - started:.1f}s (full gate before push: `mise run check`)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
