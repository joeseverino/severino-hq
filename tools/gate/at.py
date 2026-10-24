"""The test task: manage.py test on the labels named, here or at another ref.

    mise run test <label>... [manage.py test options]
    mise run test --at <ref> <label>... [manage.py test options]

With `--at`, the labels run in a throwaway worktree of the ref with this
tree's test files laid over it, so one command says whether a new test fails
on the old code, or whether a failure was already there. Test files are those
under a `tests` directory; everything else is the ref's. The worktree goes on
every exit, and the exit status is the test run's, except when a label does
not import at the ref: that run says nothing about the tests, so it has a
verdict of its own and exits 3.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

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


class RefError(Exception):
    """The ref names no commit, or its worktree cannot be made."""


def git(*args: str) -> list[str]:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if done.returncode:
        raise RefError(done.stderr.strip() or f"git {args[0]} exited {done.returncode}")
    return [line for line in done.stdout.splitlines() if line]


def is_test_file(path: str) -> bool:
    """A file the suite runs or builds its cases from, rather than the code under test."""

    parts = Path(path).parts
    return "tests" in parts[:-1]


def changed_tests(commit: str) -> tuple[list[str], list[str]]:
    """(present, deleted) test files where this tree, uncommitted work included, differs from the commit."""

    present = set(git("diff", "--name-only", "--no-renames", "--diff-filter=ACMT", commit))
    present |= set(git("ls-files", "--others", "--exclude-standard"))
    deleted = git("diff", "--name-only", "--no-renames", "--diff-filter=D", commit)
    return sorted(filter(is_test_file, present)), sorted(filter(is_test_file, deleted))


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


def run_at(ref: str, args: list[str]) -> int:
    started = time.monotonic()
    commit = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")[0]
    name = f"test at {ref} ({commit[:12]})"
    present, deleted = changed_tests(commit)
    scratch = ROOT / "var" / "test-at"
    scratch.mkdir(parents=True, exist_ok=True)
    tree = scratch / f"{commit[:12]}-{os.getpid()}"
    try:
        git("worktree", "add", "--detach", "--quiet", str(tree), commit)
        overlay(tree, present, deleted)
        # The ref's own environment: uv refuses this one's, and says so, once it is outside it.
        os.environ.pop("VIRTUAL_ENV", None)
        step = run(name, [*UV_RUN, *TEST, *args], cwd=tree)
    finally:
        remove(tree)
    laid = f", with {len(present) + len(deleted)} test files from this tree"
    if unimported(step):
        close(name, started, [step], outcome="DOES NOT IMPORT at the ref, so no test ran on its code")
        return UNIMPORTED
    close(name, started, [step], laid)
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


def stop(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str]) -> int:
    if not argv or not (argv[0] == "--at" or argv[0].startswith("--at=")):
        os.execv(sys.executable, [sys.executable, *TEST, *argv])
    ref, args = (argv[1:2], argv[2:]) if argv[0] == "--at" else ([argv[0].removeprefix("--at=")], argv[1:])
    if not ref or not ref[0]:
        print("usage: mise run test --at <ref> <label>... [manage.py test options]", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, stop)
    try:
        return run_at(ref[0], args)
    except RefError as error:
        print(f"test --at {ref[0]}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
