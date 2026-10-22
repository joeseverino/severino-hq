"""The base every gate tool's test inherits.

A gate keeps its closing verdict in `var/gate/`, where a reader whose copy of
the output was cut looks for it, so a test's fixture verdict there passes for
a real one. Each class keeps its verdicts in a folder of its own, and fails
when the real folder changed while it ran: a subprocess escapes the patch.
"""

import tempfile
from pathlib import Path
from typing import override
from unittest.mock import patch

from django.test import SimpleTestCase

from tools.gate import verdict as gate_verdict

REAL_VERDICTS = gate_verdict.VERDICTS


def verdicts_kept() -> dict[str, tuple[int, int]]:
    """Each file in the real verdict folder, by name, as its mtime and size."""

    if not REAL_VERDICTS.is_dir():
        return {}
    return {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in REAL_VERDICTS.iterdir()}


class GateTestCase(SimpleTestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.verdicts_before = verdicts_kept()
        kept = cls.enterClassContext(tempfile.TemporaryDirectory())
        cls.enterClassContext(patch.object(gate_verdict, "VERDICTS", Path(kept)))

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        try:
            after = verdicts_kept()
            if after != cls.verdicts_before:
                changed = sorted(
                    name for name in {*after, *cls.verdicts_before} if after.get(name) != cls.verdicts_before.get(name)
                )
                raise AssertionError(f"{REAL_VERDICTS} changed while {cls.__name__} ran: {', '.join(changed)}")
        finally:
            super().tearDownClass()
