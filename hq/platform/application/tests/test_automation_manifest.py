from typing import Any

from django.test import SimpleTestCase
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hq.platform.application.automation_manifest import (
    DuplicateOperation,
    OperationManifest,
    OperationRegistry,
)
from hq.platform.application.contracts import Closed


class ServiceLogs(Closed):
    service: str
    lines: int = 200


def declared(**changes: Any) -> dict[str, Any]:
    return {
        "id": "example.service_logs",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "playbook": "playbooks/service_logs.yml",
        "input_type": ServiceLogs,
        "target_kinds": ("example.machine",),
        "hosts": ("app.example.com",),
        "privilege": None,
        "effect": "read",
        "timeout_seconds": 120,
        "preconditions": ("The service is installed on the host.",),
        "verification": ("The answer holds the service's state and its last lines.",),
        "recovery": ("Nothing to recover: the procedure changes nothing.",),
        "retry": "safe",
        **changes,
    }


def manifest(**changes: Any) -> OperationManifest:
    return OperationManifest(**declared(**changes))


class InputTypeTests(SimpleTestCase):
    def test_an_execution_field_however_it_is_spelled(self) -> None:
        class PlaybookPath(Closed):
            playbook_path: str

        class Inventory(Closed):
            inventory: str

        class ExtraVars(Closed):
            extra: str = Field(alias="extra-vars")

        class Limit(Closed):
            limit: str

        for model in (PlaybookPath, Inventory, ExtraVars, Limit):
            with self.subTest(model.__name__), self.assertRaises(ValidationError):
                manifest(input_type=model)

    def test_a_nested_input_that_takes_undeclared_fields(self) -> None:
        class Loose(BaseModel):
            model_config = ConfigDict(extra="allow")

        class Outer(Closed):
            options: Loose | None = None

        with self.assertRaises(ValidationError):
            manifest(input_type=Outer)

    def test_a_free_form_field(self) -> None:
        class Bag(Closed):
            values: dict[str, str]

        class Anything(Closed):
            value: Any

        for model in (Bag, Anything):
            with self.subTest(model.__name__), self.assertRaises(ValidationError):
                manifest(input_type=model)


class RegistryTests(SimpleTestCase):
    def test_it_refuses_a_duplicate_id(self) -> None:
        with self.assertRaises(DuplicateOperation):
            OperationRegistry((manifest(), manifest(timeout_seconds=60)))
