"""fast's whole run with its selection faked, for the tests of what it runs and prints."""

import io
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from hq.platform.core.tests.task import Usage
from tools.gate import fast as fast_gate


def run_main(
    changed: tuple[str, ...] = (),
    selected: tuple[str, ...] = (),
    step: Any = None,
    printed: io.BytesIO | None = None,
    **fakes: Any,
) -> tuple[int, list[str]]:
    """fast's run on a change to ``changed``, its quick gates ``selected``, each step through ``step``
    when given, what it prints written into ``printed``: its exit code and printed lines. The gate
    judged no one tree unless ``judged`` is faked, so no run records a pass in this repository."""

    fakes = {
        "merge_base": "0" * 40,
        "changed_paths": (list(changed), []),
        "selected_tasks": selected,
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
