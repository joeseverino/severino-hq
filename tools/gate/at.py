"""The test task: manage.py test on the labels named, here or at another ref,
closing on the line tools/gate/verdict.py prints.

mise parses the task's arguments (its usage in mise-tasks/test.toml) and this
reads what it parsed. What `--at` and `--keep` do is the task's help
(`mise run test --help`). A run takes the heavy turn (mise-tasks/turn) unless
tools/gate/turn.py finds it narrow; a run at a ref builds a worktree, so it always takes it.
"""

import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from hq.config.sandbox import DATABASE, isolate
from hq.platform.application.ui import counted
from hq.platform.core.tests.task import (
    RefError,
    Usage,
    changed_tests,
    commit_of,
    git,
    is_test_file,
    overlay,
    usage,
    worktree,
)
from tests.mise_tasks import mise_tasks
from tools.gate.turn import in_turn, inside
from tools.gate.verdict import REPORTS, FailedTest, Gate, Step, run

ROOT = Path(__file__).resolve().parents[2]
# Everything after it is the developer's, so a later --parallel or -k wins.
TEST = ["manage.py", "test", "--noinput", "--parallel", "auto"]
# The ref's own dependencies, as `mise run test` installs them here.
UV_RUN = ["uv", "run", "--group", "browser", "python"]
# How the test loader reports a label whose module raised on import.
FAILED_IMPORT = "unittest.loader._FailedTest."
IMPORT_ERROR = "ImportError: Failed to import test module:"
UNIMPORTED = 3
# CPython's ImportError for a name a module lacks, which ends with the file it read.
CANNOT_IMPORT = re.compile(r"cannot import name '[^']*' from '[^']*' \((?P<path>[^)]+)\)")
# Imports the module each label names, as written, in the ref's environment
# before its suite starts. The suite imports lazily (hq/config/imports.py), so a
# name a label's module imports that the ref lacks raises only once a test, or
# a parallel worker unpickling one, first uses it: read as a failing test, or a
# pool whose workers die and restart. Imported here first, it reads as the
# label that does not import, recorded as the loader records a module that
# raised on import. A label naming no module of the tree (an option's value, a
# directory) is left to the suite. Its arguments: the record's test prefix, its
# exception's, the variable naming the record's folder, then the labels.
IMPORTS = """
import importlib, json, os, pathlib, re, sys, traceback
prefix, said, folder, *labels = sys.argv[1:]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
import django
django.setup()
records = []
for label in labels:
    parts = [part for part in re.split(r"[/.]", label.removesuffix(".py")) if part]
    found = (
        end for end in range(len(parts), 0, -1)
        if pathlib.Path(*parts[:end]).with_suffix(".py").is_file()
        or pathlib.Path(*parts[:end], "__init__.py").is_file()
    )
    if (end := next(found, None)) is None:
        continue
    try:
        importlib.import_module(".".join(parts[:end]))
    except ImportError as error:
        traceback.print_exc()
        print(f"{said} {label}", flush=True)
        records.append({"test": prefix + label, "exception": f"{said} {label}: {error}", "traceback": traceback.format_exc()})
if records and os.environ.get(folder):
    pathlib.Path(os.environ[folder], "imports.json").write_text(json.dumps(records))
sys.exit(bool(records))
"""
# Django's default discovery pattern: a file it finds tests in on its own.
TEST_MODULE = "test*.py"
# The Go module, and the Go gate's command (test:controller's) up to its packages.
GO = "controller"
GO_TEST = ["gotestsum", "--format", "standard-quiet", "--raw-command", "--", "go", "-C", GO, "test", "-json", "-race"]
# Where gotestsum writes the go test events it renders (`go doc cmd/test2json`).
GO_EVENTS = "GOTESTSUM_JSONFILE"
# The shell suites, each run through the task lint:shell runs them with, which
# says which shells a suite runs under (mise-tasks/lint/shell/suites).
SHELL_SUITES = "tests/shell"
SHELL_SUITE = "test-*.sh"
SHELL_TASK = "lint:shell:suites"
MISE = ["mise"]
# A folder of fixtures a test reads by path, from any tree: Go's convention, which go build skips
# (`go help packages`), and a Python test may read another tree's (a case table the controller shares).
TESTDATA = "testdata"
# Ends a run with the line on what its process tree spent; a run inside a measured one adds none.
MEASURER = str(ROOT / "tools" / "lib" / "measured")
# Set by the run that found itself narrow, for the run it execs under the measurer, which pops it.
FOUND_NARROW = "HQ_FOUND_NARROW"


class KeepRefused(Exception):
    """A --keep names no test file this tree changed from the ref and the ref has a copy of."""


def is_testdata(path: str) -> bool:
    """A file under a ``testdata/`` folder: support of every suite, never a test module."""

    return TESTDATA in Path(path).parts[:-1]


def is_python_test_file(path: str) -> bool:
    """A file the Python suite runs or reads: one under a ``tests/`` folder, or any tree's testdata."""

    return is_test_file(path) or is_testdata(path)


def is_go_test_file(path: str) -> bool:
    """A file the controller's tests are built from: a ``_test.go`` file, one under ``testdata/``, or one of a
    package of test support, which Go names for what it fakes (``connecttest``) or as test (``testrules``)."""

    parts = Path(path).parts
    if parts[:1] != (GO,):
        return False
    if path.endswith("_test.go") or is_testdata(path):
        return True
    return path.endswith(".go") and any(part.startswith("test") or part.endswith("test") for part in parts[1:-1])


def is_go_test_module(path: str, _labels: list[str]) -> bool:
    """A file go test compiles into its package's tests; any other Go test file is support."""

    return path.endswith("_test.go") and not is_testdata(path)


def is_shell_test_file(path: str) -> bool:
    """A file under tests/shell: a suite, or a fake or fixture one reads."""

    return Path(path).parts[:2] == tuple(Path(SHELL_SUITES).parts)


def is_shell_suite(path: str, _labels: list[str] | None = None) -> bool:
    """A suite lint:shell runs, ``tests/shell/test-*.sh``; anything else under tests/shell is its support."""

    normal = Path(os.path.normpath(path))
    return normal.parent.as_posix() == SHELL_SUITES and fnmatch.fnmatch(normal.name, SHELL_SUITE)


def shell_command(suites: list[str], tree: Path) -> list[str]:
    """The command that runs the suites as lint:shell does, read from ``tree``, the task from this checkout,
    so the gate's environment runs them."""

    return [*MISE, "-C", str(ROOT), "run", SHELL_TASK, *(str(tree / os.path.normpath(suite)) for suite in suites)]


def is_test_module(path: str, labels: list[str]) -> bool:
    """Whether ``path`` holds tests: Django finds it on its own, or a label names it or something in it."""

    if is_testdata(path):
        return False
    module = path.removesuffix(".py").replace("/", ".")
    named = (label.removesuffix(".py").replace("/", ".") for label in labels)
    return fnmatch.fnmatch(Path(path).name, TEST_MODULE) or any(
        label == module or label.startswith(f"{module}.") for label in named
    )


@dataclass
class Layout:
    """What the ref's worktree takes from this tree, and the test files it keeps as the ref has them.

    The summary names apart the support whose copy at the ref this tree's replaced, since a run that
    replaced the code a test holds tests this tree's code, not the ref's; a pass runs again with it kept
    at the ref. Which support is that code and which a fixture the new test needs is the developer's to
    say (``--keep``): a fixture held at the ref fails the new test for a reason the fix never touched,
    which reads as the proof it is not. A test module is kept the same way, so a new test can run beside
    a test module whose fakes or helpers the fix changed."""

    present: list[str]
    deleted: list[str]
    support: list[str]
    kept: list[str]
    module: tuple[str, str] = ("test module", "test modules")
    # Support this tree adds, which the ref has no copy of.
    new: list[str] = field(default_factory=list)

    def replaced(self) -> list[str]:
        """The support laid over a copy the ref has, which a second run keeps at the ref."""

        return [path for path in self.support if path not in self.new]

    def summary(self) -> str:
        modules = len(self.present) + len(self.deleted) - len(self.support)
        replaced = [f"{path} (deleted here)" if path in self.deleted else path for path in self.replaced()]
        said = counted(modules, *self.module) + " from this tree"
        if replaced:
            said += f"; not test modules, laid over the ref's own: {', '.join(replaced)} (--keep <path> runs the ref's)"
        if self.new:
            said += f"; support new in this tree: {', '.join(self.new)}"
        kept = f"; kept at the ref: {', '.join(self.kept)}" if self.kept else ""
        return f"{said}{kept}"


def layout(
    present: list[str],
    deleted: list[str],
    keep: list[str],
    labels: list[str],
    is_module: Callable[[str, list[str]], bool] = is_test_module,
    module: tuple[str, str] = ("test module", "test modules"),
    new: Collection[str] = (),
) -> Layout:
    """Every changed test file laid over the ref but those ``keep`` names, which stay as the ref has them,
    refusing a keep that names no changed test file, one the ref lacks, or the only test modules this tree
    has; ``new`` is the changed files the ref has no copy of."""

    changed = {*present, *deleted}
    support = sorted(path for path in changed if not is_module(path, labels))
    for path in keep:
        if path not in changed:
            raise KeepRefused(f"{path} is no test file this tree changed from the ref, so there is nothing to keep")
        if path in new:
            raise KeepRefused(f"{path} is new in this tree, so there is nothing at the ref to keep")
    left = [path for path in changed if is_module(path, labels) and path not in keep]
    if not left and (held := sorted({path for path in keep if is_module(path, labels)})):
        raise KeepRefused(
            f"{', '.join(held)} holds this tree's tests, so keeping it lays out none from this tree and "
            f"this run proves nothing: the proof is the same command without --keep {' --keep '.join(held)}"
        )
    return Layout(
        present=[path for path in present if path not in keep],
        deleted=[path for path in deleted if path not in keep],
        support=[path for path in support if path not in keep],
        kept=sorted(set(keep)),
        module=module,
        new=[path for path in support if path in new],
    )


@dataclass(frozen=True)
class Suite:
    """One language's tests as a run at a ref takes them: the command for its arguments in the ref's
    worktree, which changed files are its tests and which of those are support, and how a run that never
    reached the ref's code reads (``unrun`` returns what the closing line adds, or None for a run that did)."""

    command: Callable[[list[str], Path], list[str]]
    is_test: Callable[[str], bool]
    is_module: Callable[[str, list[str]], bool]
    module: tuple[str, str]
    flags: tuple[str, ...]
    outcome: str
    unrun: Callable[[Step, str, list[str], Path], str | None]
    # The variable that names a file the command writes its events to, which ``unrun`` is handed.
    events: str = ""
    # Whether a pass with support laid over the ref's own runs again with that support kept at the ref.
    # A suite whose run can't tell a fixture it lacks from a failing test would read that run's failure
    # as the proof it is not, so it never runs one.
    held_again: bool = True
    # The command that imports the labels in the ref's worktree before the suite runs, or None for none.
    imports: Callable[[list[str], Path], list[str] | None] = lambda *_: None


def import_command(labels: list[str], tree: Path) -> list[str] | None:
    """IMPORTS on the labels, with the ref's own interpreter, where the ref's suite is Django's (it has
    a manage.py, whose settings IMPORTS sets up)."""

    if not labels or not (tree / "manage.py").is_file():
        return None
    return [*UV_RUN, "-c", IMPORTS, FAILED_IMPORT, IMPORT_ERROR, REPORTS, *labels]


def python_suite() -> Suite:
    return Suite(
        imports=import_command,
        command=lambda args, _tree: [*UV_RUN, *TEST, *args],
        is_test=is_python_test_file,
        is_module=is_test_module,
        module=("test module", "test modules"),
        flags=(),
        outcome="DOES NOT IMPORT at the ref, so no test ran on its code",
        unrun=python_unrun,
    )


def go_suite() -> Suite:
    return Suite(
        command=lambda args, _tree: [*GO_TEST, *args],
        is_test=is_go_test_file,
        is_module=is_go_test_module,
        module=("Go test file", "Go test files"),
        flags=("--go",),
        outcome="DOES NOT BUILD at the ref, so no test ran on its code",
        unrun=go_unrun,
        events=GO_EVENTS,
    )


def shell_suite() -> Suite:
    """The suites the arguments name, the ref's own scripts under them; a suite reaches nothing it could
    fail to load, so every failure is the suite's. Its support is the fakes and fixtures it reads, so a
    pass is not run again with them kept at the ref: a fake the new test needs would fail it there."""

    return Suite(
        command=shell_command,
        is_test=is_shell_test_file,
        is_module=is_shell_suite,
        module=("shell suite", "shell suites"),
        flags=(),
        outcome="FAILED",
        unrun=lambda *_: None,
        held_again=False,
    )


def shell_run(typed: Typed) -> bool:
    """Whether the arguments name shell suites, refusing them beside anything else."""

    if not any(map(is_shell_suite, typed.args)):
        return False
    if typed.go or not all(map(is_shell_suite, typed.args)):
        raise ValueError(
            f"a shell suite ({SHELL_SUITES}/{SHELL_SUITE}) runs as {SHELL_TASK} runs it, so name only suites, "
            "without --go or a test option"
        )
    return True


def python_unrun(step: Step, commit: str, labels: list[str], _events: Path) -> str | None:
    if not unimported(step):
        return None
    newer = "".join(f"; {each}" for each in newer_packages(commit, labels))
    return newer + unproven(shlex.join(["mise", "run", "test", *labels]))


def go_unrun(_step: Step, _commit: str, _labels: list[str], events: Path) -> str | None:
    """The packages go test could not build, from the FailedBuild of its fail events."""

    unbuilt = sorted(set(unbuilt_packages(events)))
    if not unbuilt:
        return None
    return f"; {', '.join(unbuilt)}" + unproven("mise run test --go on those packages")


def unbuilt_packages(events: Path) -> list[str]:
    if not events.is_file():
        return []
    found = []
    for line in events.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:  # noqa: S112 - a line go test wrote that is no JSON is no event
            continue
        if isinstance(event, dict) and event.get("Action") == "fail" and event.get("FailedBuild"):
            found.append(str(event["FailedBuild"]))
    return found


def run_at(ref: str, keep: list[str], args: list[str], suite: Suite | None = None) -> int:
    suite = suite or python_suite()
    commit = commit_of(ref, ROOT)
    gate = Gate(f"test at {ref} ({commit[:12]})")
    labels = [arg for arg in args if not arg.startswith("-")]
    present, deleted = changed_tests(commit, ROOT, suite.is_test)
    laid = layout(present, deleted, keep, labels, suite.is_module, suite.module, new_at(commit, present))
    # A run folder for everything the ref's runs write, its worktree among them, as a test run has,
    # since a ref older than the gate's sandbox writes its database and scratch to TMPDIR.
    isolate(os.environ.get(DATABASE, ""))
    with worktree(commit, ROOT, lint_tools=True) as tree:
        overlay(tree, laid.present, laid.deleted, ROOT)
        # The ref's own environment: uv refuses this one's, and says so, once it is outside it.
        os.environ.pop("VIRTUAL_ENV", None)
        events = tree.parent / "events.json"
        if suite.events:
            os.environ[suite.events] = str(events)
        step = run_suite(gate.name, suite, args, labels, tree)
        unrun = suite.unrun(step, commit, labels, events)
        held = laid.replaced() if suite.held_again and step.ok and unrun is None else []
        fixtures: list[str] = []
        if held:
            # The pass ran this tree's support, so the ref's own copies go back for a run that tests the ref.
            git("--literal-pathspecs", "checkout", commit, "--", *held, root=tree)

            def again() -> tuple[Step, str | None]:
                done = run_suite(gate.name, suite, args, labels, tree)
                return done, suite.unrun(done, commit, labels, events)

            kept, held, fixtures = run_kept(again, tree, held, present)
            if kept is not None:
                step, unrun = kept
            laid = layout(present, deleted, [*keep, *held], labels, suite.is_module, suite.module, laid.new)
    said = laid.summary()
    if fixtures:
        said += (
            f"; laid over again, since the tests import from each a name the ref's copy lacks: {', '.join(fixtures)}"
        )
    keeps = (word for path in laid.kept for word in ("--keep", path))
    at_ref = ["mise", "run", "test", "--at", ref, *keeps, *suite.flags]
    again = shlex.join(at_ref)
    # A run that names no test reruns as typed.
    step.command = [*at_ref, *args]
    first = f"with this tree's {', '.join(held)} laid over instead" if held else ""
    if unrun is not None:
        failed = f"{unrun}; with {said}"
        if held:
            # That run reached no test, so it is no proof: support the new test needs is missing at the ref.
            failed += f"; {first}, it passed, so they hold a fixture the new test needs: --keep only what holds the fix"
        gate.close([step], failed=failed, outcome=suite.outcome, tests=again)
        return UNIMPORTED
    gate.close(
        [step],
        passed=f", with {said}" + (f"; and {first}" if held else ""),
        failed=f"; with {said}" + (f"; {first}, it passed" if held else ""),
        tests=again,
    )
    return step.code


def run_suite(name: str, suite: Suite, args: list[str], labels: list[str], tree: Path) -> Step:
    """The suite's run in the ref's worktree, or the import of its labels when one of them does not import
    there. An import that fails for another reason (the ref's settings) leaves the verdict to the suite."""

    if (imports := suite.imports(labels, tree)) and (step := run(name, imports, cwd=tree)).unimported:
        return step
    return run(name, suite.command(args, tree), cwd=tree)


def run_kept(
    run: Callable[[], tuple[Step, str | None]], tree: Path, held: list[str], present: list[str]
) -> tuple[tuple[Step, str | None] | None, list[str], list[str]]:
    """``run`` with ``held`` kept at the ref in ``tree``, again for as long as a failed import names one of them:
    (its step and what ``unrun`` said, or None once every one was laid over again), the files still kept, and
    the ones laid over again.

    A kept file the tests import a name from that its copy at the ref lacks, as a module loads or as a test
    runs, is a fixture the new test needs, never the fix, so it is laid over again and the rest stay at the
    ref. Once none is left, the pass with them all laid over was the ref's own."""

    fixtures: list[str] = []
    while True:
        step, unrun = run()
        needed = [] if step.ok else imported_from(step, tree, [path for path in held if path in present])
        if not needed:
            return (step, unrun), held, fixtures
        overlay(tree, needed, (), ROOT)
        fixtures += needed
        held = [path for path in held if path not in needed]
        if not held:
            return None, held, fixtures


def imported_from(step: Step, tree: Path, paths: list[str]) -> list[str]:
    """Each of ``paths`` a failed import in ``step`` named as the file that lacks the name asked of it."""

    root = tree.resolve()
    named = set()
    for failed in step.tests:
        for found in CANNOT_IMPORT.finditer(f"{failed.exception}\n{failed.traceback}"):
            if Path(found["path"]).resolve().is_relative_to(root):
                named.add(Path(found["path"]).resolve().relative_to(root).as_posix())
    return [path for path in paths if path in named]


def unproven(run: str) -> str:
    """What a run that reached none of the ref's code says: it proves nothing, and the proof that does."""

    return f"; this run proves nothing: break the fix in this tree and run {run}, which must fail, then restore the fix"


def new_at(commit: str, present: list[str]) -> list[str]:
    """The paths of ``present`` the commit has no file at."""

    return [path for path in present if not at_commit(commit, path)]


def at_commit(commit: str, path: str) -> bool:
    """Whether the commit has a file at ``path``, asked by the path itself, since a list git prints quotes some names."""

    try:
        git("cat-file", "-e", f"{commit}:{path}", root=ROOT)
    except RefError:
        return False
    return True


def unimported(step: Step) -> bool:
    """Whether a label's module raised on import at the ref; its name then stands for the module the loader tried.

    A ref whose runner writes no failure record still prints the loader's
    line, so the module is named from that."""

    failed = [
        each for each in step.tests if each.test.startswith(FAILED_IMPORT) and each.exception.startswith(IMPORT_ERROR)
    ]
    step.tests = [
        replace(each, test=each.test.removeprefix(FAILED_IMPORT)) if each in failed else each for each in step.tests
    ]
    if not step.tests and step.unimported:
        step.tests = [FailedTest(module, f"{IMPORT_ERROR} {module}") for module in step.unimported]
        return True
    return bool(failed)


def newer_packages(commit: str, labels: list[str]) -> list[str]:
    """For each label in a package of this tree the ref does not have, the
    package and the commit that adds it: the oldest ref the label runs at, since
    before it the label names a layout the ref's code does not share."""

    said = []
    for package in sorted({path for label in labels if (path := missing_package(commit, label))}):
        added = git("log", "-1", "--format=%h %s", "--diff-filter=A", "HEAD", "--", f"{package}/__init__.py", root=ROOT)
        dotted = package.replace("/", ".")
        if added:
            sha, _, subject = added[0].partition(" ")
            said.append(f"{dotted} is not at the ref; the oldest ref it runs at is {sha} ({subject[:60]})")
        else:
            said.append(f"{dotted} is not at the ref, and no commit adds it yet, so no ref before this tree runs it")
    return said


def missing_package(commit: str, label: str) -> str | None:
    """The outermost package of this tree on the label's path that the ref does not have, as a path."""

    parts = label.split(".")
    for end in range(1, len(parts) + 1):
        package = "/".join(parts[:end])
        if not (ROOT / package / "__init__.py").is_file():
            return None
        if not at_commit(commit, f"{package}/__init__.py"):
            return package
    return None


@dataclass(frozen=True)
class Typed:
    """The task's arguments as mise parsed them: ``at`` is None without --at,
    and ``args`` is the labels and manage.py test's options in the order typed."""

    at: str | None = None
    keep: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    go: bool = False

    @classmethod
    def from_usage(cls, given: Usage) -> Self:
        return cls(
            at=given.text("at") if given.given("at") else None,
            keep=list(given.repeated("keep")),
            args=list(given.repeated("arguments")),
            go=given.switch("go"),
        )


def rooted(path: str) -> str:
    """A --keep path, from the checkout's root."""

    return Path(os.path.relpath(path, ROOT) if Path(path).is_absolute() else os.path.normpath(path)).as_posix()


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def take_the_turn(typed: Typed) -> None:
    """Run again inside the heavy turn, unless this run is in it or narrow; a narrow run runs again
    under the measurer, so every run ends with its line. The run again inherits mise's parse through
    the environment."""

    # The narrow run re-run below was found narrow already; the marker is popped so no child inherits it.
    if os.environ.pop(FOUND_NARROW, None) or inside():
        return
    # Whether it is narrow is asked in a process of its own, as tools/lib/heavy-turn.sh asks: the
    # settings it loads start a run, which the exec below would leave with nothing to remove it.
    # It weighs the run this task makes: TEST's own options, then the developer's arguments.
    heavy = [sys.executable, "-m", "tools.gate.turn", *TEST[2:], *typed.args]
    if typed.at is not None or typed.go or subprocess.call(heavy, cwd=ROOT):
        # A run at a ref builds a worktree and may run the suite there twice.
        command = [*in_turn("2h"), sys.executable, "-m", "tools.gate.at"]
        os.execvp(command[0], command)
    # Measured already (a gate's run of tests): the line is the outer one's.
    if os.environ.get("HQ_MEASURED"):
        return
    os.environ[FOUND_NARROW] = "1"
    command = [MEASURER, sys.executable, "-m", "tools.gate.at"]
    os.chdir(ROOT)
    os.execv(command[0], command)


def run_here(argv: list[str], *, go: bool = False, shell: bool = False) -> int:
    gate = Gate("test")
    if go:
        command = [*GO_TEST, *argv]
    elif shell:
        command = shell_command(argv, ROOT)
    else:
        command = [sys.executable, *TEST, *argv]
    try:
        step = run("test", command)
    except KeyboardInterrupt:
        return 130
    step.command = ["mise", "run", "test", *(["--go"] if go else []), *argv]
    gate.close([step])
    return step.code


def names_code(label: str) -> bool:
    """Whether a label names something of this tree: a path, or a module or package a dotted label starts with."""

    parts = [part for part in re.split(r"[/.]", label.removesuffix(".py")) if part]
    return Path(ROOT, label).exists() or any(
        Path(ROOT, *parts[:end]).with_suffix(".py").is_file() or Path(ROOT, *parts[:end]).is_dir()
        for end in range(1, len(parts) + 1)
    )


def tasks_named(args: list[str]) -> list[str]:
    """Each label that names a mise task and nothing of this tree: a gate typed where a test goes.
    A word after an option is that option's value (``--tag browser``), never a label."""

    words = [
        arg
        for at_word, arg in enumerate(args)
        if not arg.startswith("-")
        and not (at_word and args[at_word - 1].startswith("-") and "=" not in args[at_word - 1])
        and not names_code(arg)
    ]
    return [word for word in words if word in mise_tasks()] if words else []


def refuse_tasks(typed: Typed) -> bool:
    """Refuse, before any run, a label that names a task: the test task takes tests, and a gate runs by its name."""

    if typed.go or not (tasks := tasks_named(typed.args)):
        return False
    where, on = ("mise run test", "") if typed.at is None else (f"test --at {typed.at}", f" on {typed.at}'s code")
    runs = " and ".join(f"`mise run {task}`" for task in tasks)
    print(
        f"{where}: {', '.join(tasks)} names a task, not a test. It runs tests by dotted label (hq.x.tests.test_y)"
        f" or shell suites by path (tests/shell/test-x.sh){on}, so name the tests that hold the gate's check;"
        f" the gate itself runs here as {runs}",
        file=sys.stderr,
    )
    return True


def main(typed: Typed) -> int:
    signal.signal(signal.SIGTERM, stop)
    if refuse_tasks(typed):
        return 2
    try:
        shell = shell_run(typed)
    except ValueError as error:
        print(f"mise run test: {error}", file=sys.stderr)
        return 2
    if typed.at is None:
        return run_here(typed.args, go=typed.go, shell=shell)
    if not typed.at or not all(typed.keep):
        print("mise run test: --at and --keep each need a value", file=sys.stderr)
        return 2
    suite = go_suite() if typed.go else shell_suite() if shell else python_suite()
    try:
        return run_at(typed.at, [rooted(path) for path in typed.keep], typed.args, suite)
    except (RefError, KeepRefused) as error:
        print(f"test --at {typed.at}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    typed = Typed.from_usage(usage())
    # Refused before the turn, so a mistyped label waits for nothing.
    if refuse_tasks(typed):
        sys.exit(2)
    take_the_turn(typed)
    sys.exit(main(typed))
