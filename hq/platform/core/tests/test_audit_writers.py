"""A change to a row is written once: by its model's audit signal.

A ``record_event`` that says something was created, updated, deleted or had
its settings changed writes by hand what ``register_audit`` writes for the
row, with words of its own and no field diff or subject. Each call's action is
read from the source, so a new one is found however it is reached.
"""

import ast
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.application.tests.source import source_files
from hq.platform.core.audit import audited_labels

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
    ("hq/platform/application/pins.py", "_audit"): "one act on many of a person's pins at once",
    ("hq/platform/application/action_items.py", "_audit"): "one act on many queue items at once",
    ("hq/platform/application/projects.py", "refresh_project"): "the sum of a content sync over many rows",
    ("hq/platform/core/views.py", "DemoModeView.post"): "a switch kept in the session alone",
    ("hq/platform/application/inventory.py", "_record_manages"): "a provider's own setting, as a read found it",
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


ENDINGS = frozenset({"DONE", "FAILED"})


def hand_endings(source: str) -> list[str]:
    """The enclosing definition of each ``record_event`` call in ``source`` that
    may write DONE or FAILED about a record (an ``obj``): a piece of work's end,
    which is ``audit.work_ended``'s alone."""

    found: list[str] = []

    def visit(node: ast.AST, where: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                (*where, child.name)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else where
            )
            if isinstance(child, ast.Call) and _is_record_event(child.func):
                named = {keyword.arg: keyword.value for keyword in child.keywords}
                action = named.get("action")
                members = _members(action) if action is not None else [None]
                if "obj" in named and (None in members or ENDINGS.intersection(members)):
                    found.append(".".join(where))
            visit(child, inner)

    visit(ast.parse(source), ())
    return found


@tag("source", "tree")
@reads("hq/**/*.py", "hq_sdk/**/*.py")
class WorkEndedTests(SimpleTestCase):
    def test_a_record_s_end_is_written_only_by_work_ended(self):
        found = [
            f"{path.relative_to(ROOT).as_posix()}: {where}"
            for top in ("hq", "hq_sdk")
            for path in source_files(ROOT / top, "*.py")
            if "tests" not in path.relative_to(ROOT).parts and path.relative_to(ROOT).as_posix() != WRITER
            for where in hand_endings(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(found, [], "A job's, a run's or a task's end is written by audit.work_ended alone.")

    def test_an_end_about_a_record_is_found_and_an_act_is_not(self):
        source = """
def ended(job):
    record_event(action=AuditLog.Action.DONE, obj=job)

def either(job, ok):
    record_event(action=AuditLog.Action.DONE if ok else AuditLog.Action.FAILED, obj=job)

def unknown(job, action):
    record_event(action=action, obj=job)

def refused():
    record_event(action=AuditLog.Action.FAILED, type_label="Example")

def seen(row):
    record_event(action=AuditLog.Action.OBSERVED, obj=row)
"""

        self.assertEqual(hand_endings(source), ["ended", "either", "unknown"])


# The one place a capability's handler runs, which writes the act of a write
# whose handler recorded no row (``capabilities._run``).
RUNNER = ("hq/platform/application/capabilities.py", "_run")

# Each host model no audit signal watches, and why no change to its rows is a
# person's change of their own to record, so a write to a model nobody
# registered is found here rather than passing as an act that changed nothing.
UNWATCHED = {
    "analytics.AnalyticsCoverage": "a day a credential read, a reading",
    "analytics.AnalyticsSite": "a site as the credential reports it, a reading",
    "analytics.RumDaily": "a day of traffic, a reading",
    "analytics.VitalsDaily": "a day of web vitals, a reading",
    "calendars.Preference": "a person arranging their own month, recorded as the capability's act",
    "control_plane.CertificateMaterial": "beside the sealed secret its upload writes, whose row records it",
    "control_plane.OperationHost": "an index of the machines a run's input names",
    "control_plane.ProviderConnection": "an endpoint as a controller reports it, a reading",
    "control_plane.ProviderInventory": "what a controller read, a reading",
    "control_plane.RefusedReport": "a call the bridge refused, kept until the next is accepted",
    "core.ActionItemRead": "where a person has read to, written by hand with its act",
    "core.AuditLog": "the log itself",
    "core.IdempotencyRecord": "a retry key's first answer",
    "core.Pin": "a person's pins, written by hand with its act (NOT_A_ROW)",
    "core.Revision": "a table's write count, moved by a trigger",
    "core.UpstreamReading": "a reading, recorded as observed when it changes",
    "jobs.Job": "a job's one row is its end (audit.work_ended)",
    "search_index.SearchDocument": "an index derived from the records",
}


def handler_calls(source: str) -> list[str]:
    """The enclosing definition of each call of a ``handler`` attribute in ``source``."""

    found: list[str] = []

    def visit(node: ast.AST, where: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                (*where, child.name)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else where
            )
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "handler":
                found.append(".".join(where))
            visit(child, inner)

    visit(ast.parse(source), ())
    return found


@tag("source", "tree")
@reads("hq/**/*.py", "hq_sdk/**/*.py")
class CapabilityActTests(SimpleTestCase):
    def test_a_capability_s_handler_runs_only_where_its_act_is_counted(self):
        files = [
            path
            for top in ("hq", "hq_sdk")
            for path in source_files(ROOT / top, "*.py")
            if "tests" not in path.relative_to(ROOT).parts
        ]
        self.assertTrue(files, "the walk found no source")

        calls = [
            (path.relative_to(ROOT).as_posix(), where)
            for path in files
            for where in handler_calls(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(
            calls,
            [RUNNER],
            "A capability's handler runs only in capabilities._run, which records the act of a write that wrote no row.",
        )

    def test_a_handler_call_is_found_wherever_it_is_reached(self):
        source = """
def run(spec):
    return spec.handler(command)

class Door:
    def post(self, found):
        found.spec.handler(command, principal=None)

def named(handler):
    handler(command)
"""

        self.assertEqual(handler_calls(source), ["run", "Door.post"])


class WatchedModelTests(SimpleTestCase):
    def test_every_host_model_is_watched_or_says_why_not(self):
        watched = audited_labels()
        unwatched = {
            model._meta.label
            for model in apps.get_models()
            if model.__module__.startswith("hq.") and model not in watched
        }

        self.assertEqual(
            sorted(unwatched - set(UNWATCHED)),
            [],
            "Register each with register_audit(Model, label) in its app's ready(), so a change to its rows is "
            "recorded, or name it in UNWATCHED with why no change to it is a person's own.",
        )
        self.assertEqual(sorted(set(UNWATCHED) - unwatched), [], "Each listed model is a host model no signal watches.")
