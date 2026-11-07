"""The inner-loop gate: only what the change can have broken.

What it selects from the changed files, and which quick gates it runs beside
them: docs/Development.md, What `fast` runs. ``mise run check`` stays the gate
before a push.
"""

from __future__ import annotations

import ast
import functools
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import BinaryIO

from hq.platform.application.tests.tree import SUITE, tagged
from hq.platform.core.tests.task import RefError, Usage, changed_paths, git, names, usage
from tests.mise_tasks import ROOT as TASKS_ROOT, body, mise_tasks, run_files, runs, takes_the_turn
from tools.gate.turn import in_turn
from tools.gate.verdict import ESCAPE, PREFIX, TAIL_SHOWN, TASK, Gate, Step, run
from tools.structural.classify import TEST

ROOT = Path(__file__).resolve().parents[2]
# The test run waits for the machine's heavy turn (tools.gate.turn.in_turn):
# its parallel workers hold a suite's memory for most of fast's time. A quick
# gate that takes the turn of its own (the shell gate's suites) runs after the
# tests; the rest are one short process each and run beside them, and the
# composed suites take the turn as their own task decides.
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
    "lint:mise-format": ("mise.toml", "mise-tasks/*.toml", "mise-tasks/lint/mise-format", "tools/lib/mise-files.sh"),
    # jactionlint, then zizmor over .github, each with its config there.
    "lint:workflows": (".github/workflows/*", ".github/actions/*", ".github/actionlint.yaml", ".github/zizmor.yml"),
    # mise.toml's Go pin reads controller/go.mod.
    "lint:toolchain": (
        "mise.toml",
        "mise.lock",
        "controller/go.mod",
        "Dockerfile",
        "composition/Dockerfile",
        "tools/pipeline/toolchain_lock.py",
    ),
    "lint:lockfile": ("pyproject.toml", "uv.lock"),
    # The checks are those built into the Trivy that mise.toml pins.
    "lint:trivy-config": ("*Dockerfile", "mise.toml", "tools/lint/trivy/*", "mise-tasks/lint/trivy-config"),
    "lint:deploycheck": PYTHON,
    "lint:openapi": (
        "hq/platform/api/hq-api.openapi.json",
        "controller/api/*.openapi.json",
        "tools/lint/package*.json",
    ),
    "test:migrations": ("hq/*.py",),
    "lint:drift-sdk-contract": (*PYTHON, "hq_sdk/*", "docs/Plugins.md"),
    "lint:drift-api-document": (*PYTHON, "hq/platform/api/hq-api.openapi.json"),
    "lint:drift-api-examples": (*PYTHON, "hq/platform/api/openapi-examples.json"),
    "lint:drift-bridge-contract": (*PYTHON, "controller/api/*", "hq/config/controller-connections.json"),
    "lint:drift-connections-reference": (*PYTHON, "docs/Connections.md"),
    "lint:drift-font": ("tools/fonts/*", "static/fonts/*", "static/css/*"),
}
# The shell gate reads every file its lister names, the suites among them, so a
# change selects it when the lister names a path it touched: as this tree has
# it, or as the base had it once deleted.
SHELL = "lint:shell"
SHELL_SOURCES = ROOT / "tools" / "gate" / "shell-sources.sh"
# The shell suites also read the workflows and actions, whose run blocks are
# shell the lister cannot name (tests/shell/test-shell-contracts.sh reads every
# one), so a change to them selects the gate too.
SHELL_READS = (".github/workflows/*", ".github/actions/*")
# Each lint gate a change never selects, and why: it reaches the network, so
# it is left to `mise run check`, or fast runs its check another way. A lint
# task in none of TASKS, SHELL and this is one fast would skip unseen, which
# test_fast.py fails on.
UNSELECTED: dict[str, str] = {
    "lint:audit": "queries the advisory services (uv audit, osv-scanner); left to `mise run check`",
    "lint:govulncheck": "queries vuln.go.dev for the advisories the controller reaches; left to `mise run check`",
    "lint:codeql-bundle": "fetches the pinned CodeQL action's defaults from GitHub; left to `mise run check`",
    "lint:scorecard": "its Vulnerabilities check queries OSV over an export of the tree; left to `mise run check`",
    "lint:units": "systemd-analyze is Linux's, so CI's lint job scores the units",
    "lint:patch": "fast's own patch integrity step runs `git diff --check` over the change",
    "lint:source": "fast runs the source-tagged tests among its labels, since a labeled run leaves no tag out",
    "lint:shell:suites": "runs within lint:shell, which fast selects as SHELL",
    "lint:shell:suite": "runs within lint:shell, which fast selects as SHELL",
}


def _globs(patterns: list[str]) -> re.Pattern[str]:
    """One pattern matching a path any of ``patterns`` does, each a glob over
    the path from the root, ``**`` crossing directories."""

    alternatives = [f"(?:{glob.translate(pattern, recursive=True, include_hidden=True)})" for pattern in patterns]
    return re.compile(f"^(?:{'|'.join(alternatives) or '(?!)'})")


# Paths a gate never reads, so a change to them alone leaves it out: the
# structural bar drops test code before it pairs anything, and complexipy
# leaves out what complexipy.toml's exclude names, read from there so the two
# never disagree.
UNREAD = {
    "lint:structural": TEST,
    "lint:complexity": _globs(tomllib.loads((ROOT / "complexipy.toml").read_text(encoding="utf-8"))["exclude"]),
}
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
# Run whatever changed. The startup budgets count what settings and
# django.setup import, so an import added anywhere they reach is one the
# test's own imports never select.
ALWAYS = ("architecture", "new_domain", "test_startup")
# A test that walks a directory finds a module there through no import, so a
# change to a module of any package the suite lives in runs every test tagged
# tree, in the same run as the rest: the walks read every one of them, test
# files among them (hq/platform/application/tests/test_tree_tag.py holds the
# tag to the walks and reads each test module of those packages).
TREE = tuple(f"{package}/*.py" for package in SUITE)
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


def selected_tasks(paths: list[str], base: str | None = None) -> tuple[str, ...]:
    """The quick gates a change can have broken: those whose paths it touches, or all when it touches EVERY_TASK.

    A path this tree no longer has is read as ``base`` has it, for the shell gate."""

    def touched(patterns: tuple[str, ...], task: str = "") -> bool:
        unread = UNREAD.get(task)
        read = [path for path in paths if not (unread and unread.search(path))]
        return any(fnmatch(path, pattern) for path in read for pattern in patterns)

    if touched(EVERY_TASK):
        return (*TASKS, SHELL)
    selected = tuple(task for task, patterns in TASKS.items() if touched(patterns, task))
    return (*selected, SHELL) if touched(SHELL_READS) or shell_files(paths, base) else selected


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


def tasks_command(tasks: tuple[str, ...], parallel: str, *, summary: bool = False) -> list[str]:
    """One run of every task, side by side unless parallelism is ruled out.

    `-c` names every task that failed, once the run ends. The configured
    keep-order output prints each one whole under its own name; ``summary``
    prints a task that passed as one line and one that failed whole as it
    ends, which ``Failing`` tells at once. It holds each task's output until
    that task ends, a heavy one's wait for the turn too, so it is for the
    light gates.
    """

    jobs = ["--jobs", "1"] if parallel == "1" else []
    output = ["--output", "summary"] if summary else []
    separated = [word for task in tasks for word in (":::", task)][1:]
    return ["mise", "run", "-c", *jobs, *output, *separated]


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
    """The test modules to run: those ``chosen`` picks, and the architecture tests."""

    tests = {module_name(p): p for p in files if is_test(p)}
    picked = chosen(tests, reached, touched, read) | {t for t in tests if any(a in t for a in ALWAYS)}
    return sorted(picked), len(tests)


def chosen(
    modules: dict[str, Path], reached: set[str], touched: set[str], read: list[str] | tuple[str, ...]
) -> set[str]:
    """Those of ``modules`` the change reaches by import, those that name an
    app it touched, and those that name a file it changed that is not Python
    (`read`)."""

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
    words += [pattern for path in read for pattern in path_references(path)]
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


def group_tasks(
    files: list[Path], reached: set[str], touched: set[str], read: list[str] | tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The test tasks whose group-run modules the change reaches as it would reach a test module."""

    present = {module_name(p): p for p in files}
    modules = {m: present[m] for m in group_tests() if m in present}
    picked = chosen(modules, reached, touched, read)
    return tuple(sorted({group_tests()[m] for m in picked}))


def with_tree(labels: list[str], paths: list[str]) -> list[str]:
    """The selected labels, and every test tagged tree when a module of the suite's packages changed."""

    return [*labels, *tagged(ROOT, "tree")] if reaches_tree(paths) else labels


def test_command(labels: list[str], parallel: str) -> list[str]:
    """One run of the selected tests, in the machine's heavy turn: a test both
    selected and tagged tree runs once, since Django's runner drops a
    repeated test."""

    return [*in_turn("30m"), sys.executable, "manage.py", "test", "--noinput", "--parallel", parallel, *labels]


# A line mise's summary output prints under a task that did not fail: it
# passed, or its outputs were up to date. Any other is a failed one's
# (docs/Development.md, What `fast` runs).
SETTLED = re.compile(r"^\[[^\]\s]+\] (?:passed in \S+|sources up-to-date, skipping)\s*$")


class Failing:
    """Tells each light gate that failed beside the tests as it fails, with its
    rerun, so it is fixed while the tests go on. Called as the step's output
    pauses, on the thread that writes ``held``."""

    def __init__(self, held: BinaryIO) -> None:
        self.held = held
        self.read_to = 0
        self.pending = b""
        self.said: dict[str, list[str]] = {}
        self.told: set[str] = set()

    def read_from(self, offset: int) -> None:
        """Read the step's output from ``offset`` on, past the label it is held under."""

        self.read_to = offset

    def __call__(self) -> None:
        end = self.held.tell()
        self.held.seek(self.read_to)
        *lines, self.pending = (self.pending + self.held.read(end - self.read_to)).split(b"\n")
        self.held.seek(end)
        self.read_to = end
        for raw in lines:
            line = ESCAPE.sub("", raw.decode(errors="replace"))
            if (named := PREFIX.match(line)) and not SETTLED.match(line):
                said = self.said.setdefault(named[1], [])
                if not TASK.match(line):
                    said.append(line[named.end() :])
        for task, said in self.said.items():
            if task not in self.told:
                self.told.add(task)
                self.tell(task, said)

    @staticmethod
    def tell(task: str, said: list[str]) -> None:
        ended = [" It ended on:", *(f"\n[fast]   {line}" for line in said[-TAIL_SHOWN:])] if said else []
        # One write, so a line of the tests' output never lands inside it.
        sys.stdout.write(
            f"[fast] {task} failed beside the tests, which go on.{''.join(ended)}\n"
            f"[fast] rerun it now: mise run {task}\n"
        )
        sys.stdout.flush()


# Each step running beside the main thread, by the thread it runs on.
BESIDE: dict[int, Beside] = {}


class Beside:
    """A step started on a thread of its own so the steps after it run while it
    does, its output held and printed whole, under its label, once collected,
    and each failure in it told by ``failing`` as it happens."""

    def __init__(self, label: str, cmd: list[str], output: BinaryIO, failing: Failing | None = None) -> None:
        self.output = output
        self.failing = failing
        self.called = threading.Event()
        self.result: Step | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self.run, args=(label, cmd), daemon=True)
        self.thread.start()
        # The next step starts once this one has, so the steps start in the order asked.
        self.called.wait()

    def run(self, label: str, cmd: list[str]) -> None:
        BESIDE[threading.get_ident()] = self
        try:
            self.result = step(label, cmd)
        # SystemExit would otherwise end this thread in silence; KeyboardInterrupt is
        # only ever raised on the main thread.
        except (Exception, SystemExit) as error:  # noqa: BLE001 - raised again on the main thread, in collect
            self.error = error
        finally:
            del BESIDE[threading.get_ident()]
            self.called.set()

    def collect(self) -> Step:
        self.thread.join()
        self.output.seek(0)
        sys.stdout.write(self.output.read().decode(errors="replace"))
        sys.stdout.flush()
        if self.error:
            raise self.error
        if self.result is None:
            raise RuntimeError("the step ended with neither a result nor an error")
        return self.result


def step(label: str, cmd: list[str]) -> Step:
    if beside := BESIDE.get(threading.get_ident()):
        beside.called.set()
        beside.output.write(f"[fast] {label}\n".encode())
        if beside.failing:
            beside.failing.read_from(beside.output.tell())
        return run(label, cmd, to=beside.output, paused=beside.failing)
    print(f"[fast] {label}", flush=True)
    return run(label, cmd)


def turn_takers(tasks: tuple[str, ...]) -> tuple[str, ...]:
    """Those of the quick gates that take the machine's heavy turn, which the test run holds."""

    declared = mise_tasks()
    return tuple(task for task in tasks if takes_the_turn(declared[task]))


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


def main(asked: Usage) -> int:
    # The task's usage reads CHECK_BASE and CHECK_PARALLEL behind its own arguments.
    parallel = asked.text("parallel", "auto")
    list_only = asked.switch("list")
    # A run opens on where its verdict is kept; a listing keeps none.
    gate = None if list_only else Gate("fast")
    base = merge_base(asked.text("base") or None)
    changed, deleted = changed_paths(base, ROOT)
    files = python_files()
    reached, touched = affected(changed, deleted, files)
    read = [path for path in [*changed, *deleted] if not path.endswith(".py")]
    labels, total = test_labels(files, reached, touched, read)
    every_test = len(labels) == total > 0
    print(f"[fast] base {base[:9]}: {len(changed)} changed, {len(deleted)} deleted, {len(labels)}/{total} test modules")
    if total and len(labels) / total > WIDE:
        print("[fast] wide change: most of the suite is reached; `mise run check` is the honest gate")

    if reaches_tree([*changed, *deleted]):
        print(f"[fast] a module under {', '.join(f'{p}/' for p in SUITE)} changed: every test tagged tree runs too")
    labels = with_tree(labels, [*changed, *deleted])
    if gate is None:
        print("\n".join(labels))
        return 0

    tasks = (*selected_tasks([*changed, *deleted], base), *group_tasks(files, reached, touched, read))
    after = turn_takers(tasks) if tasks else ()
    light = tuple(task for task in tasks if task not in after)
    if tasks:
        print(f"[fast] the quick gates that read what changed: {' '.join(tasks)}")
    else:
        print("[fast] no quick gate reads what changed")
    # The light gates run beside the tests, their output shown after the tests'
    # and each failure told as it happens.
    with tempfile.TemporaryFile() as held:
        quick = tasks_command(light, parallel, summary=True)
        beside = Beside("quick gates, beside the tests", quick, held, Failing(held)) if light else None
        tests = step("tests", test_command(labels, parallel))
        steps = [beside.collect(), tests] if beside else [tests]
    if after:
        steps.append(step("quick gates that take the heavy turn", tasks_command(after, parallel)))
    if touches_shared_ui([*changed, *deleted]):
        steps.extend(extension_suites(parallel))
    steps.append(step("patch integrity", ["git", "diff", "--check", base]))
    passed = all(s.ok for s in steps)
    if passed:
        record_pass(gate.judged(), base, whole=every_test and set(tasks) == {*TASKS, SHELL, *group_tests().values()})
    gate.close(steps, " (full gate before push: `mise run check`)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(usage()))
