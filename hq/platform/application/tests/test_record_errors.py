"""A record that is not there, or has moved on, is answered as itself.

Every record domain raises ``RecordNotFound`` and ``RecordConflict``; a
capability answers them as ``not_found`` and ``conflict``, in host-written
words that carry nothing the request sent.
"""

import re
import tempfile
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt

from ..capabilities import capability_registry, execute_capability
from ..integration_specs import TARGET_KINDS, kept_by
from ..security import Principal, operator_capabilities

# Distinctive enough that finding it in a message can only mean it was echoed.
SENT = "sent-by-the-caller-7f3a"
SENT_UID = "0b7e9c2e-7f3a-4c1d-9a55-3d2f6c1b8e40"
SENT_ID = 7_340_219


def operator() -> Principal:
    return Principal("operator", "cli", operator_capabilities())


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
        # Each write that keeps what it is not sent of its record reads that record first.
        cases = {
            spec.name: ({}, SENT_UID if TARGET_KINDS[spec.target_kind].coerce is str else SENT_ID)
            for spec in capability_registry()
            if kept_by(spec)
        }
        self.assertTrue(cases, "no command keeps its record")
        for name, (payload, target) in {**cases, "project.delete": ({"confirm": SENT}, SENT)}.items():
            with self.subTest(name):
                error = self.refused(name, payload, target=target)
                self.assertEqual(error["code"], "not_found")
                self.assertNotIn(str(target), error["message"])

    def test_a_missing_related_record_is_not_found_and_names_nothing_sent(self):
        error = self.refused("asset.create", {"item_name": "Server", "related_projects": [SENT]})

        self.assertEqual(error["code"], "not_found")
        self.assertNotIn(SENT, error["message"])
        self.assertFalse(Asset.objects.exists())
        # A field a record's own rule finds naming nothing is named beside its refusal.
        paid = {"date": "2026-03-01", "vendor": "A", "item": "B", "paid_from": f"project:{SENT}"}
        error = self.refused("expense.create", paid)
        self.assertEqual((error["code"], error["details"]), ("not_found", {"paid_from": ["not_found"]}))
        self.assertNotIn(SENT, error["message"])

    def test_a_stale_write_is_a_conflict_and_changes_nothing(self):
        Project.objects.create(name="Changed elsewhere", slug=SENT)
        stale = "2000-01-01T00:00:00+00:00"

        for name, payload in (("project.update", {"name": "Stale"}), ("project.delete", {"confirm": SENT})):
            with self.subTest(name):
                error = self.refused(name, payload, target=SENT, expected_updated_at=stale)
                self.assertEqual(error["code"], "conflict")
                self.assertNotIn(SENT, error["message"])
        self.assertEqual(Project.objects.get(slug=SENT).name, "Changed elsewhere")

    def test_a_stale_write_names_the_record_it_holds(self):
        stale = "2000-01-01T00:00:00+00:00"
        entry = Entry.objects.create(title="Patch the host", starts_on=date(2026, 10, 20))
        receipt = Receipt.objects.create(file="receipts/example.pdf", vendor="Example")
        cases = {
            "calendar.entry.update": ({"title": "Stale", "starts_on": "2026-10-20"}, str(entry.uid), "event"),
            "receipt.update": ({"vendor": "Stale"}, receipt.pk, "receipt"),
        }
        for name, (payload, target, word) in cases.items():
            with self.subTest(name):
                error = self.refused(name, payload, target=target, expected_updated_at=stale)
                self.assertEqual(error["code"], "conflict")
                self.assertIn(word, error["message"])
        self.assertEqual(Entry.objects.get(pk=entry.pk).title, "Patch the host")
        self.assertEqual(Receipt.objects.get(pk=receipt.pk).vendor, "Example")

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


ROOT = Path(__file__).resolve().parents[4]
# A module that builds sentences every way HQ does, and refuses text every way
# it could arrive; each line mypy must refuse ends in "# refused".
REFUSING = """
from dataclasses import dataclass, replace
from typing import Final

from hq.platform.application.record_errors import InputError, PolicyError, Sentence, worded
from hq.platform.application.record_errors import PolicyError as Renamed
from hq.platform.application.ui import counted, ended

OFF: Final = "It is switched off."


@dataclass(frozen=True)
class Command:
    name: str
    days: int
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Held:
    missing: Sentence


def refuse(why: Sentence) -> None:
    raise PolicyError(why)


def because() -> Sentence:
    return worded(OFF)


def told(command: Command) -> Sentence:
    return command.name  # refused


class Wordy(PolicyError):  # refused
    def __init__(self, reason: Sentence) -> None:
        super().__init__(reason)


def handle(command: Command, flag: bool, untyped) -> None:  # type: ignore[no-untyped-def]
    reason = worded(OFF if flag else "Not now.")
    refuse("Refused.")
    Held("That record was not found.")
    PolicyError(f"{counted(command.days, 'day')} left, {because()} {reason.lower()}")
    PolicyError(str(command))  # refused
    InputError(f"{command.name} is taken.")  # refused
    InputError(untyped.name)  # refused
    refuse(command.name)  # refused
    Held(command.name)  # refused
    replace(Held("Gone."), missing=command.name)  # refused
    Renamed(ended(f"Taken by {command.name}"))  # refused
    PolicyError(f"{counted(1, command.name)} left.")  # refused
    PolicyError(*command.parts)  # refused
"""


@tag("source")
class SentenceTypeTests(SimpleTestCase):
    """mypy, through tools/lint/mypy_sentence.py, refuses every way text HQ did not write could reach a refusal."""

    def test_mypy_refuses_each_line_that_carries_text_hq_did_not_write(self):
        from mypy import api

        with tempfile.TemporaryDirectory() as scratch:
            module = Path(scratch, "refusing.py")
            module.write_text(REFUSING)
            held = ["--enable-error-code", "return-value", "--enable-error-code", "assignment"]
            config = ["--config-file", str(ROOT / "tools/lint/mypy-reach.toml"), "--cache-dir", scratch]
            out, _, _ = api.run([*config, *held, "--no-error-summary", str(module)])

        lines = REFUSING.splitlines()
        reported = {int(found) for found in re.findall(r"refusing\.py:(\d+): error", out)}
        refused = {number for number, line in enumerate(lines, 1) if line.endswith("# refused")}
        self.assertEqual(reported, refused, out)
