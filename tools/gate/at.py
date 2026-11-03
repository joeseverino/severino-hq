"""The test task: manage.py test on the labels named, here or at another ref,
closing on the line tools/gate/verdict.py prints.

mise parses the task's arguments (its usage in mise-tasks/test.toml) and this
reads what it parsed. What `--at` and `--keep` do is the task's help
(`mise run test --help`). A run takes the heavy turn (tools/lib/heavy.sh) unless
tools/gate/turn.py finds it narrow; a run at a ref builds a worktree, so it always takes it.
"""

import fnmatch
import json
import os
import shlex
import signal
import sys
import tempfile
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

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
from tools.gate.turn import narrow
from tools.gate.verdict import FailedTest, Gate, Step, run

ROOT = Path(__file__).resolve().parents[2]
# Everything after it is the developer's, so a later --parallel or -k wins.
TEST = ["manage.py", "test", "--noinput", "--parallel", "auto"]
# The ref's own dependencies, as `mise run test` installs them here.
UV_RUN = ["uv", "run", "--group", "browser", "python"]
# How the test loader reports a label whose module raised on import.
FAILED_IMPORT = "unittest.loader._FailedTest."
IMPORT_ERROR = "ImportError: Failed to import test module:"
UNIMPORTED = 3
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
HEAVY = ROOT / "tools" / "lib" / "heavy.sh"
# heavy.sh runs its command as a process of its own; this front marks that
# process as the run that goes ahead, as tools/lib/heavy-turn.sh does a file
# task's, and execs this module in it, keeping the pid.
IN_TURN = 'SEVERINO_HEAVY_TASK=$$; export SEVERINO_HEAVY_TASK; exec "$@"'


class KeepRefused(Exception):
    """A --keep names no test file this tree changed from the ref and the ref has a copy of."""


def is_go_test_file(path: str) -> bool:
    """A file the controller's tests are built from: a ``_test.go`` file, one under ``testdata/``, or one of a
    package of test support, which Go names for what it fakes (``connecttest``) or as test (``testrules``)."""

    parts = Path(path).parts
    if parts[:1] != (GO,):
        return False
    if path.endswith("_test.go") or "testdata" in parts[:-1]:
        return True
    return path.endswith(".go") and any(part.startswith("test") or part.endswith("test") for part in parts[1:-1])


def is_go_test_module(path: str, _labels: list[str]) -> bool:
    """A file go test compiles into its package's tests; any other Go test file is support."""

    return path.endswith("_test.go")


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
    refusing a keep that names no changed test file or one the ref lacks; ``new`` is the changed files the
    ref has no copy of."""

    changed = {*present, *deleted}
    support = sorted(path for path in changed if not is_module(path, labels))
    for path in keep:
        if path not in changed:
            raise KeepRefused(f"{path} is no test file this tree changed from the ref, so there is nothing to keep")
        if path in new:
            raise KeepRefused(f"{path} is new in this tree, so there is nothing at the ref to keep")
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


def python_suite() -> Suite:
    return Suite(
        command=lambda args, _tree: [*UV_RUN, *TEST, *args],
        is_test=is_test_file,
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
        except ValueError:
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
    with worktree(commit, ROOT, lint_tools=True) as tree, tempfile.TemporaryDirectory(prefix="test-at-") as scratch:
        overlay(tree, laid.present, laid.deleted, ROOT)
        # The ref's own environment: uv refuses this one's, and says so, once it is outside it.
        os.environ.pop("VIRTUAL_ENV", None)
        events = Path(scratch, "events.json")
        if suite.events:
            os.environ[suite.events] = str(events)
        step = run(gate.name, suite.command(args, tree), cwd=tree)
        unrun = suite.unrun(step, commit, labels, events)
        held = laid.replaced() if suite.held_again and step.ok and unrun is None else []
        if held:
            # The pass ran this tree's support, so the ref's own copies go back for a run that tests the ref.
            git("--literal-pathspecs", "checkout", commit, "--", *held, root=tree)
            laid = layout(present, deleted, [*keep, *held], labels, suite.is_module, suite.module, laid.new)
            step = run(gate.name, suite.command(args, tree), cwd=tree)
            unrun = suite.unrun(step, commit, labels, events)
    said = laid.summary()
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
    """Run again inside the heavy turn, unless this is that run or a narrow one.
    The run in the turn inherits mise's parse through the environment."""

    if os.environ.get("SEVERINO_HEAVY_TASK") == str(os.getpid()):
        return
    if typed.at is not None or typed.go or not narrow(typed.args):
        os.execv(HEAVY, [str(HEAVY), "sh", "-c", IN_TURN, "sh", sys.executable, "-m", "tools.gate.at"])


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


def main(typed: Typed) -> int:
    signal.signal(signal.SIGTERM, stop)
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
    take_the_turn(typed)
    sys.exit(main(typed))
