"""fast's whole run with its selection faked, for the tests of what it runs and prints."""

import io
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from hq.platform.core.tests.task import Usage
from tools.gate import fast as fast_gate, verdict as gate_verdict


def run_main(
    changed: tuple[str, ...] = (),
    selected: tuple[str, ...] = (),
    step: Any = None,
    printed: io.BytesIO | None = None,
    **fakes: Any,
) -> tuple[int, list[str]]:
    """fast's run on a change to ``changed``, its quick gates ``selected``, each step through ``step``
    when given, what it prints written into ``printed``: its exit code and printed lines. Each changed path
    reads as added, so a tree test that lists it runs. The gate
    judged no one tree unless ``judged`` is faked, so no run records a pass or a map in this
    repository, and no tree passed and no map is read unless ``passed_ancestor`` or
    ``recorded_ancestor`` is faked; no fix mode runs unless ``fix`` is."""

    fakes = {
        "merge_base": "0" * 40,
        "passed_ancestor": None,
        "recorded_ancestor": None,
        "record_map": None,
        "changed_paths": (list(changed), []),
        "listed_change": list(changed),
        "selected_tasks": selected,
        "fix": [],
        **fakes,
    }
    judged = fakes.pop("judged", None)
    out = io.TextIOWrapper(printed or io.BytesIO(), write_through=True)
    with ExitStack() as stack:
        for name, value in fakes.items():
            stack.enter_context(patch.object(fast_gate, name, return_value=value))
        stack.enter_context(patch.object(fast_gate.Gate, "judged", return_value=judged))
        if step is not None:
            stack.enter_context(patch.object(fast_gate, "step", side_effect=step))
        stack.enter_context(patch("sys.stdout", new=out))
        code = fast_gate.main(Usage({}))
    return code, out.buffer.getvalue().decode().splitlines()


class Recorder:
    """A step that prints before it ends, as every real one does, and records what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, label: str, cmd: list[str]) -> gate_verdict.Step:
        self.calls.append((label, cmd))
        print("step output", flush=True)
        return gate_verdict.Step(label)
