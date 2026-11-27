"""What one capability call answers, whatever adapter carried it."""

from dataclasses import dataclass

from django.test import TestCase

from ..capabilities import CapabilitySpec, execute_capability
from ..integration_testing import commands_registered
from ..security import Capability, cli_principal


@dataclass(frozen=True, slots=True)
class ExampleCommand:
    pass


class CapabilityCallTests(TestCase):
    def setUp(self):
        self.ran = []

    def handler(self, command, **_bound):
        self.ran.append(_bound)
        return {"ok": True}

    def call(self, spec, payload, principal=None, **carried):
        with commands_registered(spec):
            return execute_capability(spec.name, payload, principal=principal or cli_principal(), **carried)

    def test_a_capability_that_takes_a_target_refuses_a_call_without_one(self):
        spec = CapabilitySpec(
            "example.records.update",
            "Change example records.",
            "remote_write",
            Capability.READ,
            ExampleCommand,
            self.handler,
            target_kind="slug",
        )
        result = self.call(spec, {})

        self.assertEqual(result["error"]["code"], "target_required")
        self.assertEqual(self.ran, [])
