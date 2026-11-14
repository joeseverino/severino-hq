"""A change to a row is written once: by its model's audit signal.

A ``record_event`` that says something was created, updated, deleted or had
its settings changed writes by hand what ``register_audit`` writes for the
row, with words of its own and no field diff or subject. Each call's action is
read from the source, so a new one is found however it is reached.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.application.tests.source import source_files

ROOT = Path(__file__).resolve().parents[4]
CHANGES = frozenset({"CREATED", "UPDATED", "DELETED", "SETTINGS_CHANGED"})
# The writer itself, whose signal receivers write the changes.
WRITER = "hq/platform/core/audit.py"
THE_ONE_WAY = (
    "writes a change by hand: register the model with register_audit(Model, label) in its app's ready() "
    "and set audit_said (audit_gone for a delete) on the row before it is saved, so its signal writes the row"
)

# Each call that still writes a change, or an action read from elsewhere, and why no row's signal can write it.
NOT_A_ROW = {
    ("hq/platform/application/glance.py", "select_dashboard_machine"): "the dashboard's choice, kept in no row",
    ("hq/platform/application/glance.py", "save_dashboard_settings"): "the dashboard's settings, kept in no row",
    ("hq/platform/application/glance.py", "_ask_for"): "a refresh asked for, kept in no row",
    ("hq/platform/application/pins.py", "_audit"): "one act on many of a person's pins at once",
    ("hq/platform/application/action_items.py", "_audit"): "one act on many queue items at once",
    ("hq/platform/application/projects.py", "refresh_project"): "the sum of a content sync over many rows",
    ("hq/platform/core/views.py", "DemoModeView.post"): "a switch kept in the session alone",
    ("hq/platform/application/inventory.py", "_record_manages"): "a provider's own setting, as a read found it",
    ("hq/domains/jobs/runner.py", "_audit"): "how a job ended, never a change",
    ("hq/platform/application/controller.py", "_settle_run"): "how a run's tasks ended, never a change",
}


def hand_changes(source: str) -> list[tuple[str, str]]:
    """Each ``record_event`` call in ``source`` whose action is a change, or is
    not named at the call: its enclosing definition and the action as written."""

    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, where: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                (*where, child.name)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else where
            )
            if isinstance(child, ast.Call) and _is_record_event(child.func):
                action = next((keyword.value for keyword in child.keywords if keyword.arg == "action"), None)
                if action is not None and not _names_only_acts(action):
                    found.append((".".join(where), ast.unparse(action)))
            visit(child, inner)

    visit(ast.parse(source), ())
    return found


def _is_record_event(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "record_event") or (
        isinstance(func, ast.Attribute) and func.attr == "record_event"
    )


def _names_only_acts(action: ast.expr) -> bool:
    """Whether ``action`` is written at the call as ``AuditLog.Action`` members, none of them a change."""

    members = _members(action)
    return None not in members and not CHANGES.intersection(members)


def _members(action: ast.expr) -> list[str | None]:
    """Each action ``action`` may be, by its member's name, or None for one read from elsewhere."""

    if isinstance(action, ast.IfExp):
        return [*_members(action.body), *_members(action.orelse)]
    value = action.value if isinstance(action, ast.Attribute) else None
    if isinstance(value, ast.Attribute) and value.attr == "Action":
        return [action.attr]
    return [None]


@tag("source", "tree")
@reads("hq/**/*.py")
class HandWrittenChangeTests(SimpleTestCase):
    def test_no_change_to_a_row_is_written_by_hand(self):
        files = [path for path in source_files(ROOT / "hq", "*.py") if "tests" not in path.relative_to(ROOT).parts]
        self.assertTrue(files, "the walk found no source")

        seen: set[tuple[str, str]] = set()
        found: list[str] = []
        for path in files:
            name = path.relative_to(ROOT).as_posix()
            if name == WRITER:
                continue
            for where, action in hand_changes(path.read_text(encoding="utf-8")):
                if (name, where) in NOT_A_ROW:
                    seen.add((name, where))
                else:
                    found.append(f"{name}: {where or '(module)'} records {action}")

        self.assertEqual(found, [], f"Each {THE_ONE_WAY}.")
        self.assertEqual(sorted(set(NOT_A_ROW) - seen), [], "Each listed call matches a call that writes by hand.")

    def test_a_change_or_an_action_from_elsewhere_is_found_and_an_act_is_not(self):
        source = """
def kept(row):
    record_event(action=AuditLog.Action.UPDATED, obj=row, message="Renamed")

class Settings:
    def post(self, request):
        audit.record_event(action=AuditLog.Action.CREATED if request else AuditLog.Action.DELETED)

def ended(action):
    record_event(action=action)

def seen():
    record_event(action=AuditLog.Action.VIEWED)
    record_event(action=AuditLog.Action.DONE if True else AuditLog.Action.FAILED)
"""

        self.assertEqual([where for where, _action in hand_changes(source)], ["kept", "Settings.post", "ended"])
