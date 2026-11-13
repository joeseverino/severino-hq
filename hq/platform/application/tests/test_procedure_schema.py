"""The published schema of a procedure's sidecar judges a sidecar as HQ does,
and `manage.py procedure_schema --check` fails while the file is behind."""

import json
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, tag
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .. import procedure_schema
from ..automation_sources import ProcedureDeclaration

PLAYBOOKS = Path(__file__).resolve().parents[4] / "hq_runner" / "tests" / "example_project" / "playbooks"

ACCEPTED: dict[str, Any] = {
    "id": "example.restart",
    "target_kinds": ["machine"],
    "effect": "infrastructure_change",
    "privilege": "example.service_control",
    "timeout_seconds": 300,
    "retry": "observe_first",
    "recovery": ["Start it by hand."],
    "inputs": {"unit": {"pattern": "^[a-z]+$", "max_length": 64}},
}


def model_accepts(declaration: Any) -> bool:
    try:
        # As HQ reads a sidecar the worker reported: from JSON.
        ProcedureDeclaration.model_validate_json(json.dumps(declaration))
    except ValidationError:
        return False
    return True


class SchemaTests(SimpleTestCase):
    validator: Draft202012Validator

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        published = json.loads(procedure_schema.render())
        Draft202012Validator.check_schema(published)
        cls.validator = Draft202012Validator(published)

    def assertJudgedAlike(self, declaration: Any, *, accepted: bool) -> None:
        self.assertEqual(model_accepts(declaration), accepted, declaration)
        self.assertEqual(self.validator.is_valid(declaration), accepted, declaration)

    def test_a_sidecar_hq_accepts_the_schema_accepts(self):
        self.assertJudgedAlike(ACCEPTED, accepted=True)
        least = {key: value for key, value in ACCEPTED.items() if key not in {"privilege", "inputs"}}
        self.assertJudgedAlike(least, accepted=True)

    def test_what_hq_refuses_of_a_sidecar_the_schema_refuses(self):
        refused = {
            "a field HQ has no word for": {**ACCEPTED, "playbook": "site.yml"},
            "no id": {key: value for key, value in ACCEPTED.items() if key != "id"},
            "an id outside the dotted names": {**ACCEPTED, "id": "Example Restart"},
            "an effect that is none of the three": {**ACCEPTED, "effect": "sometimes"},
            "no target kind": {**ACCEPTED, "target_kinds": []},
            "a timeout past the longest": {**ACCEPTED, "timeout_seconds": 3601},
            "a timeout as text": {**ACCEPTED, "timeout_seconds": "300"},
            "an input named outside the pattern": {**ACCEPTED, "inputs": {"Unit": ACCEPTED["inputs"]["unit"]}},
            "an input bound with a field of its own": {
                **ACCEPTED,
                "inputs": {"unit": {**ACCEPTED["inputs"]["unit"], "default": "web"}},
            },
            "an input with no length bound": {**ACCEPTED, "inputs": {"unit": {"pattern": "^[a-z]+$"}}},
        }
        for case, declaration in refused.items():
            with self.subTest(case):
                self.assertJudgedAlike(declaration, accepted=False)

    @tag("source")
    def test_the_example_project_s_sidecars_are_judged_as_hq_judges_them(self):
        import yaml

        sidecars = sorted(PLAYBOOKS.glob("*.hq.yml"))
        self.assertTrue(sidecars, f"no sidecar under {PLAYBOOKS}")
        judged = {}
        for sidecar in sidecars:
            declaration = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            accepted = model_accepts(declaration)
            judged[sidecar.name] = accepted
            with self.subTest(sidecar.name):
                self.assertEqual(self.validator.is_valid(declaration), accepted)
        # The project holds sidecars of both kinds, so each side is read.
        self.assertEqual(set(judged.values()), {True, False}, judged)


class CheckTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, procedure_schema.SCHEMA_PATH.name)
        patcher = patch.object(procedure_schema, "SCHEMA_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self) -> None:
        call_command("procedure_schema", "--check", stdout=StringIO())

    def test_the_current_schema_passes(self):
        call_command("procedure_schema", stdout=StringIO())
        self.check()

    def test_a_schema_behind_the_model_fails_and_is_left_as_it_was(self):
        behind = json.loads(procedure_schema.render())
        del behind["properties"]["inputs"]
        stale = json.dumps(behind, indent=2, sort_keys=True) + "\n"
        self.path.write_text(stale, encoding="utf-8")

        with self.assertRaises(CommandError):
            self.check()
        self.assertEqual(self.path.read_text(encoding="utf-8"), stale)

    def test_a_missing_schema_fails(self):
        with self.assertRaises(CommandError):
            self.check()
        self.assertFalse(self.path.exists())
