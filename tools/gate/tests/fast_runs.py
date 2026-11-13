"""fast's whole run with its selection faked, for the tests of what it runs and prints."""

import io
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from hq.platform.core.tests.task import Usage
from tools.gate import chain, fast as fast_gate, selection, verdict as gate_verdict

# The modules fast is split into, each holding its own binding of a name it imports.
MODULES = (fast_gate, selection, chain)


@contextmanager
def everywhere(name: str, *new: Any, **mocked: Any) -> Iterator[Any]:
    """``name`` patched in each of fast's modules that holds it, to ``new`` or else to one mock made of
    ``mocked``, so a fake reaches every caller wherever it looks the name up; it yields what it put there."""

    value = new[0] if new else MagicMock(**mocked)
    with ExitStack() as stack:
        for module in MODULES:
            if hasattr(module, name):
                stack.enter_context(patch.object(module, name, value))
        yield value


def run_main(
    changed: tuple[str, ...] = (),
    selected: tuple[str, ...] = (),
    step: Any = None,
    printed: io.BytesIO | None = None,
    asked: dict[str, str] | None = None,
    **fakes: Any,
) -> tuple[int, list[str]]:
    """fast's run on a change to ``changed``, its quick gates ``selected``, each step through ``step``
    when given, what it prints written into ``printed``: its exit code and printed lines. Each changed path
    reads as added, so a tree test that lists it runs. The gate
    judged no one tree unless ``judged`` is faked, so no run records a pass or a map in this
    repository, and no tree passed and no map is read unless ``passed_ancestor`` or
    ``recorded_ancestor`` is faked; no fix mode runs unless ``fix`` is, and GitHub is never asked whether CI
    passed the base unless ``passed_in_ci`` is faked. ``asked`` holds the task's arguments, as mise parsed them."""

    fakes = {
        "merge_base": "0" * 40,
        "passed_ancestor": None,
        "recorded_ancestor": None,
        "record_map": None,
        "changed_paths": (list(changed), []),
        "listed_change": list(changed),
        "selected_tasks": selected,
        "fix": [],
        "passed_in_ci": "CI was not asked",
        **fakes,
    }
    judged = fakes.pop("judged", None)
    out = io.TextIOWrapper(printed or io.BytesIO(), write_through=True)
    with ExitStack() as stack:
        for name, value in fakes.items():
            stack.enter_context(everywhere(name, return_value=value))
        stack.enter_context(patch.object(fast_gate.Gate, "judged", return_value=judged))
        if step is not None:
            stack.enter_context(everywhere("step", side_effect=step))
        stack.enter_context(patch("sys.stdout", new=out))
        code = fast_gate.main(Usage(asked or {}))
    return code, out.buffer.getvalue().decode().splitlines()


class Recorder:
    """A step that prints before it ends, as every real one does, and records what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, label: str, cmd: list[str]) -> gate_verdict.Step:
        self.calls.append((label, cmd))
        print("step output", flush=True)
        return gate_verdict.Step(label)
