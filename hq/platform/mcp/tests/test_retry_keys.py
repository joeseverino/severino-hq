"""An MCP write is as safe to retry as an API write: it carries a key, and a repeat acts once."""

from unittest import mock

from django.test import TestCase

from hq.domains.projects.models import Project
from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.security import Capability, Principal
from hq_sdk.capabilities import StrictCommand

from .. import services
from ..identity import set_principal

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects"}))
READER = Principal("example-reader", "mcp", frozenset({"read"}))
PROJECT = {"name": "Once", "slug": "once", "status": "active"}


class RetryKeyTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(WRITER))

    def test_a_write_without_a_key_is_refused_and_acts_on_nothing(self):
        for payload in (PROJECT, PROJECT | {"idempotency_key": ""}):
            with self.subTest(payload=payload):
                result = services.execute_capability("project.create", payload)

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "idempotency_key_required")
        self.assertFalse(Project.objects.filter(slug="once").exists())

    def test_a_repeat_with_the_same_key_returns_the_first_answer_and_acts_once(self):
        payload = PROJECT | {"idempotency_key": "example-retry-1"}

        first = services.execute_capability("project.create", payload)
        again = services.execute_capability("project.create", payload)

        self.assertTrue(first["ok"], first)
        self.assertEqual(again, first)
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)

    def test_a_caller_who_may_not_write_is_told_that_rather_than_asked_for_a_key(self):
        self.enterContext(set_principal(READER))

        result = services.execute_capability("project.create", PROJECT)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(Project.objects.filter(slug="once").exists())

    def test_a_read_takes_no_key(self):
        self.enterContext(set_principal(READER))
        ran = []
        spec = CapabilitySpec(
            "example.records.look",
            "Look at example records.",
            "read",
            Capability.READ,
            StrictCommand,
            lambda _command, **_bound: ran.append(True) or {"ok": True},
        )
        self.enterContext(
            mock.patch("hq.platform.application.capabilities.capability_registry", return_value={spec.name: spec})
        )

        result = services.execute_capability(spec.name, {})

        self.assertTrue(result["ok"], result)
        self.assertEqual(ran, [True])
