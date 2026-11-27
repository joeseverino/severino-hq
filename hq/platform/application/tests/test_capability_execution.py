"""How a capability runs: who may call it, what it checks, and what it records."""

from dataclasses import dataclass
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase


class CapabilityTests(TestCase):
    def test_a_validator_that_refuses_in_words_of_its_own_does_not_load(self):
        from typing import Annotated

        from pydantic import AfterValidator, create_model, field_validator

        from ..contracts import Closed
        from ..integration_specs import command_adapter

        @dataclass(frozen=True, slots=True)
        class WordyCommand:
            token: Annotated[str, AfterValidator(str.strip)]

        with self.assertRaises(TypeError):
            command_adapter(WordyCommand)

        def _token(cls, value):
            return value

        with self.assertRaises(TypeError):
            create_model(
                "WordyModel", __base__=Closed, __validators__={"_token": field_validator("token")(_token)}, token=str
            )

    def test_the_operator_call_refuses_a_malformed_request(self):
        from django.core.management.base import CommandError

        for raw in (
            "not json",
            "[]",
            '{"tool": 3}',
            '{"tool": "no_such_tool"}',
            '{"tool": "audit_registry", "arguments": []}',
        ):
            with (
                self.subTest(raw=raw),
                mock.patch("sys.stdin", StringIO(raw)),
                self.assertRaises(CommandError),
            ):
                call_command("hq_call", stdout=StringIO())
