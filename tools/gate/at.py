"""The test task: manage.py test on the labels named, here or at another ref,
closing on the line tools/gate/verdict.py prints.

mise parses the task's arguments (its usage in mise-tasks/test.toml) and this
reads what it parsed. What `--at` and `--keep` do is the task's help
(`mise run test --help`). A run takes the heavy turn (tools/lib/heavy.sh) unless
tools/gate/turn.py finds it narrow; a run at a ref builds a worktree, so it always takes it.
"""

import fnmatch
import os
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from hq.platform.application.ui import counted
from hq.platform.core.tests.task import RefError, Usage, changed_tests, commit_of, overlay, usage, worktree
from tools.gate.turn import narrow
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
HEAVY = ROOT / "tools" / "lib" / "heavy.sh"
# heavy.sh runs its command as a process of its own; this front marks that
# process as the run that goes ahead, as tools/lib/heavy-turn.sh does a file
# task's, and execs this module in it, keeping the pid.
IN_TURN = 'SEVERINO_HEAVY_TASK=$$; export SEVERINO_HEAVY_TASK; exec "$@"'


class KeepRefused(Exception):
    """A --keep names no support file this tree changed from the ref."""


def is_test_module(path: str, labels: list[str]) -> bool:
    """Whether ``path`` holds tests: Django finds it on its own, or a label names it or something in it."""

    module = path.removesuffix(".py").replace("/", ".")
    named = (label.removesuffix(".py").replace("/", ".") for label in labels)
    return fnmatch.fnmatch(Path(path).name, TEST_MODULE) or any(
        label == module or label.startswith(f"{module}.") for label in named
    )


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


def run_at(ref: str, keep: list[str], args: list[str]) -> int:
    started = time.monotonic()
    commit = commit_of(ref, ROOT)
    name = f"test at {ref} ({commit[:12]})"
    laid = layout(*changed_tests(commit, ROOT), keep, [arg for arg in args if not arg.startswith("-")])
    with worktree(commit, ROOT) as tree:
        overlay(tree, laid.present, laid.deleted, ROOT)
        # The ref's own environment: uv refuses this one's, and says so, once it is outside it.
        os.environ.pop("VIRTUAL_ENV", None)
        step = run(name, [*UV_RUN, *TEST, *args], cwd=tree)
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
        each for each in step.tests if each.test.startswith(FAILED_IMPORT) and each.exception.startswith(IMPORT_ERROR)
    ]
    step.tests = [
        replace(each, test=each.test.removeprefix(FAILED_IMPORT)) if each in failed else each for each in step.tests
    ]
    return bool(failed)


@dataclass(frozen=True)
class Typed:
    """The task's arguments as mise parsed them: ``at`` is None without --at,
    and ``args`` is the labels and manage.py test's options in the order typed."""

    at: str | None = None
    keep: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)

    @classmethod
    def from_usage(cls, given: Usage) -> Self:
        return cls(
            at=given.text("at") if given.given("at") else None,
            keep=list(given.repeated("keep")),
            args=list(given.repeated("arguments")),
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
    if typed.at is not None or not narrow(typed.args):
        os.execv(HEAVY, [str(HEAVY), "sh", "-c", IN_TURN, "sh", sys.executable, "-m", "tools.gate.at"])


def run_here(argv: list[str]) -> int:
    started = time.monotonic()
    try:
        step = run("test", [sys.executable, *TEST, *argv])
    except KeyboardInterrupt:
        return 130
    close("test", started, [step])
    return step.code


def main(typed: Typed) -> int:
    signal.signal(signal.SIGTERM, stop)
    if typed.at is None:
        return run_here(typed.args)
    if not typed.at or not all(typed.keep):
        print("mise run test: --at and --keep each need a value", file=sys.stderr)
        return 2
    try:
        return run_at(typed.at, [rooted(path) for path in typed.keep], typed.args)
    except (RefError, KeepRefused) as error:
        print(f"test --at {typed.at}: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    typed = Typed.from_usage(usage())
    take_the_turn(typed)
    sys.exit(main(typed))
