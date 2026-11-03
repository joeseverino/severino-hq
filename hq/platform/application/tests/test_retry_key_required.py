"""The application, not a door, refuses a change sent without a retry key.

A client holding a token retries what a lost connection left unanswered, and
a browser resends a form, so each of their changes carries a key. Only the
shell is not asked.
"""

from django.test import TestCase

from hq.domains.projects.models import Project
from hq.platform.core.models import IdempotencyRecord

from ..capabilities import execute_capability
from ..security import DEVICE_INTERFACE, Capability, Principal, cli_principal

PROJECT = {"name": "Once", "slug": "once", "status": "active"}
GRANT = frozenset({Capability.READ, "write_projects"})


class RequiredTests(TestCase):
    def create(self, principal, **payload):
        return execute_capability("project.create", PROJECT | payload, principal=principal)

    def test_a_change_from_any_other_interface_without_a_key_is_refused_and_writes_nothing(self):
        for interface in ("web", "api", "mcp", DEVICE_INTERFACE, "example-new-interface"):
            for payload in ({}, {"idempotency_key": ""}):
                with self.subTest(interface=interface, payload=payload):
                    result = self.create(Principal("example-client", interface, GRANT), **payload)

                    self.assertEqual(result["error"]["code"], "idempotency_key_required")
        self.assertFalse(Project.objects.exists())

    def test_a_caller_who_may_not_change_it_is_told_that_first(self):
        result = self.create(Principal("example-reader", "mcp", frozenset({Capability.READ})))

        self.assertEqual(result["error"]["code"], "forbidden")

    def test_the_shell_still_runs_without_one(self):
        self.assertTrue(self.create(cli_principal())["ok"])
        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_a_token_change_with_a_key_runs_once(self):
        agent = Principal("example-client", "api", GRANT)

        first = self.create(agent, idempotency_key="example-once")
        again = self.create(agent, idempotency_key="example-once")

        self.assertTrue(first["ok"], first)
        self.assertEqual(again, first)
        self.assertEqual(Project.objects.count(), 1)
