"""The inner-loop gate: only what the change can have broken.

Changed files against the merge base are mapped to the test modules that reach
them, by the import graph of the tree itself (nothing here lists an app). The
architecture tests always run. ```mise run check``` stays the gate before a push.

Usage: mise run fast [--list] [BASE]
BASE defaults to $CHECK_BASE, then origin/main, then main. --list prints the
selected test modules and stops.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Never part of the Django suite: other languages, vendored trees, the browser
# pass that `mise run check` also leaves to `mise run browser`.
SKIP_PARTS = {"node_modules", "controller", "browser_tests", ".venv", "staticfiles"}
ALWAYS = ("architecture", "new_domain")
# Past this share of the suite the change is wide and the fast gate says so.
WIDE = 0.6


def git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line]


def merge_base(base: str | None) -> str:
    for candidate in (base, os.environ.get("CHECK_BASE"), "origin/main", "main"):
        if not candidate:
            continue
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except (subprocess.CalledProcessError, IndexError):
            continue
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
    return sorted(
        Path(f) for f in found
        if (ROOT / f).exists() and not SKIP_PARTS & set(Path(f).parts)
    )


def is_test(path: Path) -> bool:
    return path.suffix == ".py" and path.name != "__init__.py" and (path.name.startswith("test") or "fuzz" in path.parts)


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
                for assignment in node.body if isinstance(assignment, ast.Assign)
                for target in assignment.targets if isinstance(target, ast.Name)
                if isinstance(assignment.value, ast.Constant)
                and isinstance(assignment.value.value, str)
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

    A change in an app also selects every test that imports any of the app: a
    test that drives its views through the client imports its models, never
    its views.
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
            seen.update(i for m in members for i in reverse[m])
    visited: set[str] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        seen.add(current)
        stack.extend(reverse.get(current, ()))
    return seen, touched


def test_labels(files: list[Path], reached: set[str], touched: set[str]) -> tuple[list[str], int]:
    by_module = {module_name(p): p for p in files if is_test(p)}
    tests = set(by_module)
    chosen = {t for t in tests if t in reached}
    # A test that names an app (a URL namespace, a label) exercises it.
    words = [re.compile(rf"\b{re.escape(app)}\b") for app in touched]
    for name, path in by_module.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        if any(word.search(text) for word in words):
            chosen.add(name)
    chosen |= {t for t in tests if any(a in t for a in ALWAYS)}
    return sorted(chosen), len(tests)


def mypy_targets(changed: list[str]) -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
    listed = config["files"]
    roots = [entry.rstrip("/") + "/" for entry in listed]
    # Files named on the command line skip mypy's own exclude, so apply it here.
    exclude = re.compile(config.get("exclude", "(?!)"))
    return [
        c
        for c in changed
        if c.endswith(".py")
        and not exclude.search(c)
        and (c in listed or c.startswith(tuple(roots)))
    ]


def step(label: str, cmd: list[str], env: dict[str, str] | None = None) -> bool:
    print(f"[fast] {label}", flush=True)
    started = time.monotonic()
    code = subprocess.run(cmd, cwd=ROOT, env=env).returncode
    print(f"[fast] {label}: {time.monotonic() - started:.1f}s", flush=True)
    return code == 0


def main(argv: list[str]) -> int:
    started = time.monotonic()
    python = os.environ.get("FAST_PYTHON") or sys.executable
    ruff = os.environ.get("FAST_RUFF") or "ruff"
    list_only = "--list" in argv
    args = [a for a in argv if a != "--list"]
    base = merge_base(args[0] if args else None)
    changed, deleted = changed_files(base)
    files = python_files()
    reached, touched = affected(changed, deleted, files)
    labels, total = test_labels(files, reached, touched)
    print(
        f"[fast] base {base[:9]}: {len(changed)} changed, {len(deleted)} deleted, "
        f"{len(labels)}/{total} test modules"
    )
    if total and len(labels) / total > WIDE:
        print("[fast] wide change: most of the suite is reached; `mise run check` is the honest gate")

    if list_only:
        print("\n".join(labels))
        return 0

    env = {**os.environ, "DJANGO_DEBUG": "true", "SEVERINO_LOG_LEVEL": "CRITICAL"}
    ok = True
    py = [c for c in changed if c.endswith(".py") or c.endswith(".pyi")]
    if py:
        ok &= step("ruff (changed files)", [ruff, "check", *py])
    ok &= step("manage.py check", [python, "manage.py", "check"], env)
    ok &= step(
        "migration drift", [python, "manage.py", "makemigrations", "--check", "--dry-run"], env
    )
    ok &= step(
        "OpenAPI drift",
        [python, "manage.py", "api_openapi", "--check"],
        {key: value for key, value in env.items() if key != "SEVERINO_HQ_PLUGINS"},
    )
    ok &= step(
        "controller contract drift", [python, "manage.py", "bridge_contract", "--check"], env
    )
    typed = mypy_targets(changed)
    if typed:
        ok &= step("mypy (changed typed modules)", [python, "-m", "mypy", *typed], env)
    ok &= step(
        "tests", [python, "manage.py", "test", "--noinput", "--parallel", os.environ.get("CHECK_PARALLEL", "auto"), *labels], env
    )
    ok &= step("patch integrity", ["git", "diff", "--check", base])
    print(f"[fast] {'passed' if ok else 'FAILED'} in {time.monotonic() - started:.1f}s (full gate before push: `mise run check`)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
