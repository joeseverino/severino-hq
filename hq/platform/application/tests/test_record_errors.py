"""A record that is not there, or has moved on, is answered as itself.

Every record domain raises ``RecordNotFound`` and ``RecordConflict``; a
capability answers them as ``not_found`` and ``conflict``, in host-written
words that carry nothing the request sent.
"""

import ast
import textwrap
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt

from .. import approvals
from ..capabilities import execute_capability
from ..record_errors import HostRefusal
from ..security import OPERATOR_CAPABILITIES, Principal
from .refusal_text import check

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


def _sources() -> dict[str, ast.Module]:
    """Every module under hq/ outside tests, by its path in the repository."""

    return {
        str(path.relative_to(ROOT)): ast.parse(path.read_text(), filename=str(path))
        for path in (ROOT / "hq").rglob("*.py")
        if "tests" not in path.parts
    }


def _subclasses(cls: type) -> set[type]:
    found = set(cls.__subclasses__())
    return found | {deeper for sub in found for deeper in _subclasses(sub)}


@tag("source", "tree")
class RefusalTextTests(SimpleTestCase):
    """A capability answers with a refusal's text, so every refusal is built from a sentence HQ wrote."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.checked = check(_sources())

    def test_every_refusal_is_built_from_a_sentence(self):
        checked = self.checked

        self.assertEqual(checked.problems, [])
        self.assertTrue(checked.refusals_built, "no refusal was found under hq/")
        self.assertTrue(checked.carried, "no sentence was carried to a refusal under hq/")

    def test_every_kind_of_refusal_is_read(self):
        """Read from the source, so a kind is checked the day it is declared; here, every kind Python knows."""

        declared = {
            cls.__name__ for cls in _subclasses(HostRefusal) if not cls.__module__.rpartition(".")[0].endswith("tests")
        }

        self.assertIn(approvals.UnreadableSubject.__name__, declared)
        self.assertLessEqual(declared, self.checked.index.refusals)


# A stand-in for record_errors, and a module that refuses with every kind of
# sentence and passes; each planted case is appended to it.
ERRORS = """
Sentence = str


class HostRefusal(Exception):
    def __init__(self, reason: Sentence) -> None:
        super().__init__(reason)
        self.reason = reason


class PolicyError(HostRefusal, ValueError):
    pass


class InputError(HostRefusal, ValueError):
    pass
"""
REFUSING = """
from dataclasses import dataclass, replace

from pydantic import BaseModel, ConfigDict

from hq.example.errors import InputError, PolicyError, Sentence
from hq.platform.application.ui import counted

LIMIT = 20
OFF = "It is switched off."


@dataclass(frozen=True)
class Held:
    missing: Sentence

    def not_found(self):
        return InputError(self.missing)


def refuse(why: Sentence) -> None:
    raise PolicyError(why)


def why() -> Sentence:
    return "Because."


def allowed(left) -> tuple[bool, Sentence]:
    return False, f"{counted(left, 'day')} left, of {LIMIT}."


def handle(command, record, flag):
    ok, explanation = allowed(command.days)
    reason = OFF if flag else why()
    refuse("Refused.")
    Held("That record was not found.")
    raise PolicyError(f"{explanation} {reason} It is {record.get_state_display().lower()}.")
"""
PLANTED = {
    "a dependency's text": "raise PolicyError(str(command))",
    "a request value in an f-string": 'raise InputError(f"{command.name} is taken.")',
    "a kind of refusal declared anywhere": "class PlantedRefusal(InputError):\n    pass\n\nPlantedRefusal(command.name)",
    "a sentence parameter given a value": "refuse(command.name)",
    "a sentence field given a value": "Held(command.name)",
    "a sentence field replaced by a value": 'replace(Held("Gone."), missing=command.name)',
    "a sentence function returning a value": "def told() -> Sentence:\n    return command.name",
    "a sentence tuple returning a value": "def pair() -> tuple[bool, Sentence]:\n    return True, command.name",
    "a refusal renamed on import": "from hq.example.errors import PolicyError as Refused",
    "a mutable class holding a sentence": "@dataclass\nclass Loose:\n    missing: Sentence",
    "a pydantic sentence class built from data": (
        "class Locked(BaseModel):\n    model_config = ConfigDict(frozen=True)\n    reason: Sentence = ''\n\n"
        "Locked.model_validate(command)"
    ),
    "a formatter of HQ's name declared elsewhere": (
        "def counted(n, word):\n    return str(n)\n\nraise PolicyError(f'{counted(command, \"day\")} left.')"
    ),
    "a formatter given its words as a value": "raise PolicyError(f'{counted(1, command.word)} left.')",
    "a local name bound once to a value": (
        "def bound(command):\n    reason = 'Refused.'\n    reason = command.name\n    raise PolicyError(reason)"
    ),
    "a refusal given more than its sentence": "raise PolicyError(*command.parts)",
    "a refusal with a constructor of its own": (
        "class Wordy(PolicyError):\n    def __init__(self, reason, extra):\n        super().__init__(reason)"
    ),
}


def _planted(case: str) -> dict[str, ast.Module]:
    body = REFUSING + "\n\n" + textwrap.dedent(case)
    return {"hq/example/errors.py": ast.parse(ERRORS), "hq/example/refusing.py": ast.parse(body)}


class RefusalCheckerTests(SimpleTestCase):
    """The checker finds each way a refusal could carry text HQ did not write."""

    def test_a_module_built_from_sentences_passes(self):
        checked = check(_planted(""))

        self.assertEqual(checked.problems, [])
        self.assertTrue(checked.carried)

    def test_each_planted_violation_is_found(self):
        for name, case in PLANTED.items():
            with self.subTest(name):
                problems = check(_planted(case)).problems
                self.assertTrue(problems, f"{name} passed")
                self.assertTrue(all(p.startswith("hq/example/refusing.py:") for p in problems), problems)
