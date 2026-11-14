"""How a piece of work stands is said through one table: ``work_runs.TONES``
for its tone, ``JobState``'s labels for its word, and ``work_runs.STANDS``
for a control that follows it. A second table keyed by work states is a
second answer, free to color or word one job two ways on two pages."""

import ast
import re
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.domains.control_plane.models import OperationState
from hq.domains.jobs.models import JobState

from .. import work_runs
from .facts import Found, scan

HOME = Path(work_runs.__file__).resolve()
SOURCE = Path(__file__).resolve().parents[3]
# A work state as a string, and as a member of the states' enums.
VALUES = {str(state) for state in (*JobState, *OperationState)}
MEMBERS = {state.name for state in (*JobState, *OperationState)}
_KEY = (
    rf"^(['\"]({'|'.join(map(re.escape, sorted(VALUES)))})['\"]"
    rf"|([A-Za-z_]\w*\.)*({'|'.join(sorted(MEMBERS))}))$"
)
RULES = {"table": {"kind": "dictionary", "has": {"kind": "pair", "has": {"field": "key", "regex": _KEY}}}}


def _names_a_state(key: ast.expr | None) -> bool:
    if isinstance(key, ast.Constant):
        return key.value in VALUES
    if isinstance(key, ast.Attribute):
        return key.attr in MEMBERS
    return isinstance(key, ast.Name) and key.id in MEMBERS


def _tables(found: list[Found]) -> list[str]:
    tables = []
    for each in found:
        if {"tests", "migrations"} & set(each.path.parts) or each.path == HOME:
            continue
        node = ast.parse(f"({each.text})", mode="eval").body
        if isinstance(node, ast.Dict) and sum(map(_names_a_state, node.keys)) > 1:
            tables.append(f"{each.path.name}:{each.line}")
    return sorted(tables)


def restated(*paths: Path) -> list[str]:
    """Where a module other than ``work_runs`` keys a table by two work states or more."""

    return _tables(scan(RULES, *paths))


@tag("source", "tree")
class WorkStateWordsTests(SimpleTestCase):
    def test_no_other_module_keys_a_table_by_work_states(self):
        """Read the word, tone or standing from ``work_runs`` (``TONES``, ``STANDS``, ``job_run``)."""

        self.assertEqual(restated(SOURCE), [])

    def test_a_table_keyed_by_work_states_is_found_however_its_keys_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "example.py"
            module.write_text(
                f'TONES = {{"{JobState.QUEUED}": "neutral", "{JobState.RUNNING}": "neutral"}}\n'
                'WORDS = {JobState.SUCCEEDED: "Done", JobState.FAILED: "Failed"}\n'
                'SAID = {QUEUED: "Waiting", RUNNING: "Running", "": ""}\n'
                'NESTED = {Job.State.LOST: "attention", OperationRequest.State.CLAIMED: "active"}\n'
                f'ONE = {{"{JobState.FAILED}": "serious", "other": "good"}}\n'
            )

            self.assertEqual(restated(module), ["example.py:1", "example.py:2", "example.py:3", "example.py:4"])
