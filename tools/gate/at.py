"""The test task: manage.py test on the labels named, here or at another ref.

    mise run test <label>... [manage.py test options]
    mise run test --at <ref> [--keep <path>]... <label>... [manage.py test options]

With `--at`, the labels run in a throwaway worktree of the ref with this
tree's test files laid over it, so one command says whether a new test fails
on the old code, or whether a failure was already there. Test files are those
under a `tests` directory; everything else is the ref's. A test module
(`test*.py`, or one a label names) always comes from this tree. Any other test
file (a gate, a probe, a fixture) is support: laid over too, so the new test
imports, unless `--keep <path>` leaves it as the ref has it, which is how a
check whose logic lives in support is proven. The closing line counts the test
modules laid over and names each support file laid over or kept.

The worktree goes on every exit, and the exit status is the test run's, except
when a label does not import at the ref: that run says nothing about the
tests, so it has a verdict of its own and exits 3.
"""

import argparse
import fnmatch
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hq.platform.application.ui import counted
from tools.gate.verdict import Step, close, run

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
USAGE = "usage: mise run test --at <ref> [--keep <path>]... <label>... [manage.py test options]"


class RefError(Exception):
    """The ref names no commit, or its worktree cannot be made."""


class KeepRefused(Exception):
    """A --keep names no support file this tree changed from the ref."""


def git(*args: str) -> list[str]:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode:
        raise RefError(done.stderr.strip() or f"git {args[0]} exited {done.returncode}")
    return [line for line in done.stdout.splitlines() if line]


def is_test_file(path: str) -> bool:
    """A file the suite runs or builds its cases from, rather than the code under test."""

    parts = Path(path).parts
    return "tests" in parts[:-1]


def is_test_module(path: str, labels: list[str]) -> bool:
    """Whether ``path`` holds tests: Django finds it on its own, or a label names it or something in it."""

    module = path.removesuffix(".py").replace("/", ".")
    named = (label.removesuffix(".py").replace("/", ".") for label in labels)
    return fnmatch.fnmatch(Path(path).name, TEST_MODULE) or any(
        label == module or label.startswith(f"{module}.") for label in named
    )


def changed_tests(commit: str) -> tuple[list[str], list[str]]:
    """(present, deleted) test files where this tree, uncommitted work included, differs from the commit."""

    present = set(git("diff", "--name-only", "--no-renames", "--diff-filter=ACMT", commit))
    present |= set(git("ls-files", "--others", "--exclude-standard"))
    deleted = git("diff", "--name-only", "--no-renames", "--diff-filter=D", commit)
    return sorted(filter(is_test_file, present)), sorted(filter(is_test_file, deleted))


@dataclass
class Layout:
    """What the ref's worktree takes from this tree, and the support it keeps as the ref has it."""

    present: list[str]
    deleted: list[str]
    support: list[str]
    kept: list[str]

    def summary(self) -> str:
        modules = len(self.present) + len(self.deleted) - len(self.support)
        support = ", ".join(f"{path} (deleted here)" if path in self.deleted else path for path in self.support)
        laid = counted(modules, "test module", "test modules") + (f" and support {support}" if support else "")
        kept = f"; kept at the ref: {', '.join(self.kept)}" if self.kept else ""
        return f"{laid} from this tree{kept}"


def layout(present: list[str], deleted: list[str], keep: list[str], labels: list[str]) -> Layout:
    """Every changed test file laid over the ref but the support ``keep`` names,
    refusing a keep that names no changed support."""

    changed = {*present, *deleted}
    support = sorted(path for path in changed if not is_test_module(path, labels))
    for path in keep:
        if path in changed and path not in support:
            raise KeepRefused(
                f"{path} is a test module, so it comes from this tree; logic of its own that the new test "
                "checks is held at the ref only once it moves to a support module beside it"
            )
        if path not in support:
            raise KeepRefused(f"{path} is no test file this tree changed from the ref, so there is nothing to keep")
    return Layout(
        present=[path for path in present if path not in keep],
        deleted=[path for path in deleted if path not in keep],
        support=[path for path in support if path not in keep],
        kept=sorted(set(keep)),
    )


def overlay(tree: Path, present: list[str], deleted: list[str]) -> None:
    for path in present:
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, tree / path)
    for path in deleted:
        (tree / path).unlink(missing_ok=True)


def remove(tree: Path) -> None:
    """The worktree and git's record of it; what a half-made one leaves goes with the directory."""

    subprocess.run(["git", "worktree", "remove", "--force", str(tree)], cwd=ROOT, capture_output=True)
    shutil.rmtree(tree, ignore_errors=True)


def run_at(ref: str, keep: list[str], args: list[str]) -> int:
    started = time.monotonic()
    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")[0]
    name = f"test at {ref} ({commit[:12]})"
    laid = layout(*changed_tests(commit), keep, [arg for arg in args if not arg.startswith("-")])
    scratch = ROOT / "var" / "test-at"
    scratch.mkdir(parents=True, exist_ok=True)
    tree = scratch / f"{commit[:12]}-{os.getpid()}"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), commit)
        overlay(tree, laid.present, laid.deleted)
        # The ref's own environment: uv refuses this one's, and says so, once it is outside it.
        os.environ.pop("VIRTUAL_ENV", None)
        step = run(name, [*UV_RUN, *TEST, *args], cwd=tree)
    finally:
        remove(tree)
    said = laid.summary()
    if unimported(step):
        close(
            name,
            started,
            [step],
            failed=f"; with {said}",
            outcome="DOES NOT IMPORT at the ref, so no test ran on its code",
        )
        return UNIMPORTED
    close(name, started, [step], passed=f", with {said}", failed=f"; with {said}")
    return step.code


def unimported(step: Step) -> bool:
    """Whether a label's module raised on import at the ref; its name then stands for the module the loader tried."""

    failed = [
        test
        for test in step.tests
        if test.startswith(FAILED_IMPORT) and any(line.startswith(IMPORT_ERROR) for line in step.reports.get(test, ()))
    ]
    for test in failed:
        module = test.removeprefix(FAILED_IMPORT)
        step.tests[step.tests.index(test)] = module
        step.reports[module] = step.reports.pop(test, [])
    return bool(failed)


# Only --at and --keep are this task's; every other argument, in the order
# typed, is the labels and manage.py test's options. Without abbreviation, so
# a shortened option meant for manage.py is never taken for one of these.
ARGS = argparse.ArgumentParser(
    prog="mise run test", usage=USAGE.removeprefix("usage: "), allow_abbrev=False, exit_on_error=False
)
ARGS.add_argument("--at", required=True)
ARGS.add_argument("--keep", action="append", default=[])


def rooted(path: str) -> str:
    """A --keep path, from the checkout's root."""

    return Path(os.path.relpath(path, ROOT) if Path(path).is_absolute() else os.path.normpath(path)).as_posix()


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> int:
    if not argv or not (argv[0] == "--at" or argv[0].startswith("--at=")):
        os.execv(sys.executable, [sys.executable, *TEST, *argv])
    try:
        options, args = ARGS.parse_known_args(argv)
    except argparse.ArgumentError as error:
        print(f"{USAGE}\n{error}", file=sys.stderr)
        return 2
    if not options.at or not all(options.keep):
        print(f"{USAGE}\n--at and --keep each need a value", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, stop)
    try:
        return run_at(options.at, [rooted(path) for path in options.keep], args)
    except (RefError, KeepRefused) as error:
        print(f"test --at {options.at}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
