"""What one capability call answers, whatever adapter carried it."""

from unittest import mock

from django.test import TestCase

from hq.platform.api.models import IdempotencyRecord
from hq.platform.api.views import CURRENT_API_VERSION
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog
from hq_sdk.capabilities import StrictCommand

from ..capabilities import CapabilitySpec, describe_capabilities, execute_capability
from ..security import Capability, cli_principal


class ExampleCommand(StrictCommand):
    pass


class ExampleConnectionCommand(StrictCommand):
    connection_ref: str = ""


class CapabilityCallTests(TestCase):
    def setUp(self):
        self.ran = []

    def handler(self, command, **_bound):
        self.ran.append(_bound)
        return {"ok": True}

    def registered(self, *specs):
        return mock.patch(
            "hq.platform.application.capabilities.capability_registry",
            return_value={spec.name: spec for spec in specs},
        )

    def call(self, spec, payload, target=None):
        with self.registered(spec):
            return execute_capability(spec.name, payload, principal=cli_principal(), target=target)

    def spec(self, name="example.records.change", *, effect="remote_write", command=ExampleCommand, target_kind=None):
        return CapabilitySpec(
            name, "Change example records.", effect, Capability.READ, command, self.handler, target_kind=target_kind
        )

    def test_the_description_is_of_the_current_api(self):
        described = describe_capabilities()

        self.assertIs(described["ok"], True)
        self.assertEqual(described["schema_version"], CURRENT_API_VERSION)

    def test_a_capability_that_takes_no_target_refuses_one_and_runs_nothing(self):
        result = self.call(self.spec(), {}, target="example")

        self.assertEqual(result["error"]["code"], "target_not_allowed")
        self.assertEqual(self.ran, [])

    def test_a_capability_that_takes_a_target_refuses_a_call_without_one(self):
        result = self.call(self.spec(target_kind="slug"), {})

        self.assertEqual(result["error"]["code"], "target_required")
        self.assertEqual(self.ran, [])

    def test_a_payload_that_is_not_an_object_is_invalid_input(self):
        for effect in ("read", "remote_write"):
            with self.subTest(effect=effect):
                result = self.call(self.spec(effect=effect), [])

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(self.ran, [])

    def test_a_keyed_call_is_stored_as_a_success(self):
        result = self.call(self.spec(), {"idempotency_key": "example-once"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(IdempotencyRecord.objects.get().status_code, 200)

    def test_what_a_command_naming_a_connection_does_is_attributed_to_it(self):
        def handler(command, **_bound):
            record_event(action=AuditLog.Action.UPDATED, type_label="Example", message="Looked.", required=True)
            return {"ok": True}

        spec = CapabilitySpec(
            "example.gateway.look", "Look.", "read", Capability.READ, ExampleConnectionCommand, handler
        )

        self.assertTrue(self.call(spec, {"connection_ref": "example-gateway"})["ok"])

        self.assertEqual(AuditLog.objects.get(object_type="Example").connection, "example-gateway")
