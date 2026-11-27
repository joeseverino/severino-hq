"""Schema-to-form projection tests; canonical validation remains authoritative."""

from dataclasses import dataclass

from django.test import SimpleTestCase

from hq.platform.application.command_forms import command_form_class


class CapabilityCommandFormTests(SimpleTestCase):
    def test_a_field_for_agents_is_left_off_the_form_and_one_it_must_be_sent_is_refused(self):
        from typing import Annotated

        from hq.platform.application.command_fields import ForAgents
        from hq.platform.application.integration_specs import CapabilitySpec

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            note: str = ""
            trace: Annotated[str, ForAgents()] = ""

        @dataclass(frozen=True, slots=True)
        class ExampleAsked:
            trace: Annotated[str, ForAgents()]

        def spec(command):
            return CapabilitySpec("example.check.run", "Check it.", "remote_write", "example.write", command, dict)

        self.assertEqual(set(command_form_class(spec(ExampleCommand)).payload_names), {"note"})
        with self.assertRaises(ValueError):
            command_form_class(spec(ExampleAsked))
