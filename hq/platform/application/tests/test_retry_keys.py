"""One retry convention for every capability that changes or queues something.

Each takes an optional ``idempotency_key``, declared once from its effect; a
repeat carrying the same key returns the first result. A ``read`` takes none.
"""

from pathlib import Path
import tempfile

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ReadRequest
from hq.platform.api.models import IdempotencyRecord

from ..adoption_testing import managing_everything
from ..capabilities import capability_registry, describe_capabilities, execute_capability
from ..integration_specs import IDEMPOTENCY_FIELD, capability_schema
from ..security import Principal, cli_principal

REFRESH = "infrastructure.controller.refresh"
RECONCILE = "infrastructure.reconcile"
DIRECTORY = Path(tempfile.mkdtemp())
MARKERS = override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
)


class DeclaredOnceTests(TestCase):
    def test_every_capability_that_is_not_a_read_accepts_the_key_and_none_requires_it(self):
        for name, spec in capability_registry().items():
            schema = capability_schema(spec)
            with self.subTest(capability=name):
                if spec.effect == "read":
                    self.assertNotIn(IDEMPOTENCY_FIELD, schema.get("properties", {}))
                    continue
                self.assertIn(IDEMPOTENCY_FIELD, schema["properties"])
                self.assertNotIn(IDEMPOTENCY_FIELD, schema.get("required", ()))

    def test_the_published_schema_is_the_one_execution_validates_against(self):
        described = {item["name"]: item for item in describe_capabilities()["capabilities"]}

        for name in (REFRESH, RECONCILE):
            schema = described[name]["input_schema"]
            with self.subTest(capability=name):
                self.assertEqual(schema["properties"][IDEMPOTENCY_FIELD]["type"], "string")
                self.assertNotIn(IDEMPOTENCY_FIELD, schema.get("required", ()))
                self.assertFalse(schema["additionalProperties"])


@MARKERS
class RefreshTests(TestCase):
    def ask(self, principal=None, **payload):
        return execute_capability(REFRESH, payload, principal=principal or cli_principal())

    def test_it_accepts_a_key(self):
        result = self.ask(every_connection=True, idempotency_key="refresh-once")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["requested"])

    def test_it_still_runs_without_one_and_records_nothing_to_replay(self):
        self.assertTrue(self.ask(every_connection=True)["ok"])

        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_a_repeat_with_the_same_key_returns_the_first_result_and_asks_once(self):
        first = self.ask(every_connection=True, idempotency_key="refresh-once")
        ReadRequest.objects.all().delete()

        second = self.ask(every_connection=True, idempotency_key="refresh-once")

        self.assertEqual(second, first)
        self.assertFalse(ReadRequest.objects.exists())

    def test_another_key_acts_again(self):
        self.ask(every_connection=True, idempotency_key="refresh-once")
        ReadRequest.objects.all().delete()

        self.ask(every_connection=True, idempotency_key="refresh-twice")

        self.assertEqual(ReadRequest.objects.count(), 1)

    def test_the_same_key_for_a_different_request_is_a_conflict(self):
        self.ask(every_connection=True, idempotency_key="refresh-once")

        result = self.ask(kind="tailscale.device", idempotency_key="refresh-once")

        self.assertEqual(result["error"]["code"], "idempotency_conflict")

    def test_a_key_belongs_to_its_actor(self):
        self.ask(every_connection=True, idempotency_key="shared-text")
        ReadRequest.objects.all().delete()
        other = Principal("another-operator", "cli", cli_principal().capabilities)

        result = self.ask(principal=other, every_connection=True, idempotency_key="shared-text")

        self.assertTrue(result["ok"], result)
        self.assertEqual(ReadRequest.objects.count(), 1)

    def test_a_refused_request_leaves_its_key_free_for_the_corrected_one(self):
        refused = self.ask(connection_ref="no-such-connection", idempotency_key="refresh-once")
        self.assertEqual(refused["error"]["code"], "invalid_input")
        self.assertFalse(IdempotencyRecord.objects.exists())

        self.assertTrue(self.ask(every_connection=True, idempotency_key="refresh-once")["ok"])

    def test_a_malformed_key_is_invalid_input(self):
        for key in ("", "has a space", "x" * 129, 7):
            with self.subTest(key=key):
                result = self.ask(every_connection=True, idempotency_key=key)

                self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertFalse(ReadRequest.objects.exists())

    def test_a_field_the_command_does_not_have_is_still_refused(self):
        result = self.ask(every_connection=True, idempotency_keys="refresh-once")

        self.assertEqual(result["error"]["code"], "invalid_input")


class ReadTests(TestCase):
    def test_a_read_takes_no_key(self):
        result = execute_capability(
            "lookup.name",
            {"hostname": "example.com", "idempotency_key": "look-once"},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")


@MARKERS
class ReconcileTests(TestCase):
    """A command that stores the key with what it queues takes it the same way."""

    KEY = "example-rewrite"

    def setUp(self):
        managing_everything()
        ManagedResource.objects.create(
            key=self.KEY,
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "192.0.2.10"},
        )

    def ask(self, **payload):
        return execute_capability(
            RECONCILE, payload, principal=cli_principal(), target=self.KEY
        )

    def test_it_queues_without_a_key(self):
        result = self.ask()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["queued"])
        self.assertTrue(OperationRequest.objects.get().idempotency_key.startswith("command:"))

    def test_the_callers_key_is_the_one_the_operation_keeps(self):
        self.ask(idempotency_key="reconcile-once")

        self.assertEqual(OperationRequest.objects.get().idempotency_key, "reconcile-once")

    def test_a_repeat_with_the_same_key_returns_the_first_result(self):
        first = self.ask(idempotency_key="reconcile-once")

        second = self.ask(idempotency_key="reconcile-once")

        self.assertEqual(second, first)
        self.assertEqual(OperationRequest.objects.count(), 1)
