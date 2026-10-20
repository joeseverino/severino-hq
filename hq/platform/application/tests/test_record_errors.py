"""A record that is not there, or has moved on, is answered as itself.

Every record domain raises ``RecordNotFound`` and ``RecordConflict``; a
capability answers them as ``not_found`` and ``conflict``, in host-written
words that carry nothing the request sent.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project

from ..capabilities import execute_capability
from ..security import OPERATOR_CAPABILITIES, Principal

# Distinctive enough that finding it in a message can only mean it was echoed.
SENT = "sent-by-the-caller-7f3a"
SENT_UID = "0b7e9c2e-7f3a-4c1d-9a55-3d2f6c1b8e40"
SENT_ID = 7_340_219


def operator() -> Principal:
    return Principal("operator", "cli", OPERATOR_CAPABILITIES)


class RecordRefusalTests(TestCase):
    def refused(self, name, payload, *, target=None, expected_updated_at=None):
        result = execute_capability(
            name,
            payload,
            principal=operator(),
            target=target,
            expected_updated_at=expected_updated_at,
        )
        self.assertFalse(result["ok"], result)
        return result["error"]

    def test_a_missing_record_is_not_found_and_names_nothing_sent(self):
        cases = {
            "project.update": ({"name": "Renamed"}, SENT),
            "project.delete": ({"confirm": SENT}, SENT),
            "asset.update": ({"item_name": "Renamed"}, SENT),
            "content.update": ({"title": "Renamed"}, SENT),
            "calendar.entry.update": ({"title": "Renamed", "starts_on": "2026-10-20"}, SENT_UID),
            "documentation.update": ({"doc_id": "rb-renamed", "title": "Renamed"}, SENT),
            "expense.update": ({"date": "2026-10-20", "vendor": "Example", "item": "Disk"}, SENT_ID),
            "receipt.update": ({"vendor": "Example"}, SENT_ID),
        }
        for name, (payload, target) in cases.items():
            with self.subTest(name):
                error = self.refused(name, payload, target=target)
                self.assertEqual(error["code"], "not_found")
                self.assertNotIn(str(target), error["message"])

    def test_a_missing_related_record_is_not_found_and_names_nothing_sent(self):
        error = self.refused("asset.create", {"item_name": "Server", "related_projects": [SENT]})

        self.assertEqual(error["code"], "not_found")
        self.assertNotIn(SENT, error["message"])
        self.assertFalse(Asset.objects.exists())

    def test_a_stale_write_is_a_conflict_and_changes_nothing(self):
        Project.objects.create(name="Changed elsewhere", slug=SENT)
        stale = "2000-01-01T00:00:00+00:00"

        for name, payload in (("project.update", {"name": "Stale"}), ("project.delete", {"confirm": SENT})):
            with self.subTest(name):
                error = self.refused(name, payload, target=SENT, expected_updated_at=stale)
                self.assertEqual(error["code"], "conflict")
                self.assertNotIn(SENT, error["message"])
        self.assertEqual(Project.objects.get(slug=SENT).name, "Changed elsewhere")

    def test_a_current_write_runs(self):
        project = Project.objects.create(name="HQ", slug="hq")

        result = execute_capability(
            "project.update",
            {"name": "Renamed"},
            principal=operator(),
            target=project.slug,
            expected_updated_at=project.updated_at.isoformat(),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(Project.objects.get(pk=project.pk).name, "Renamed")


def _refusals(tree: ast.AST):
    names = {"RecordNotFound", "RecordConflict"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in names:
                yield node.exc


@tag("source")
class RefusalTextTests(SimpleTestCase):
    def test_every_refusal_is_a_fixed_sentence(self):
        """A capability answers with this text, so it can carry no value from the request."""

        root = Path(__file__).resolve().parents[4] / "hq"
        checked = 0
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for call in _refusals(ast.parse(path.read_text(), filename=str(path))):
                checked += 1
                fixed = not call.keywords and (
                    not call.args
                    or (
                        len(call.args) == 1
                        and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)
                    )
                )
                self.assertTrue(fixed, f"{path.relative_to(root.parent)}:{call.lineno} builds its refusal text")
        self.assertTrue(checked, "no RecordNotFound or RecordConflict raise was found under hq/")
