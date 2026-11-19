"""The application, not a door, refuses a change sent without a retry key.

A client holding a token retries what a lost connection left unanswered, and
a browser resends a form, so each of their changes carries a key. Only the
shell is not asked.
"""

from django.test import TestCase

from hq.domains.projects.models import Project

from ..capabilities import execute_capability
from ..security import DEVICE_INTERFACE, Capability, Principal

PROJECT = {"name": "Once", "slug": "once", "status": "active"}
GRANT = frozenset({Capability.READ, "write_projects"})


class RequiredTests(TestCase):
    def create(self, principal):
        return execute_capability("project.create", PROJECT, principal=principal)

    def test_a_change_from_any_other_interface_without_a_key_is_refused_and_writes_nothing(self):
        for interface in ("web", "api", "mcp", DEVICE_INTERFACE, "example-new-interface"):
            with self.subTest(interface=interface):
                result = self.create(Principal("example-client", interface, GRANT))

                self.assertEqual(result["error"]["code"], "idempotency_key_required")
        self.assertFalse(Project.objects.exists())

    def test_a_caller_who_may_not_change_it_is_told_that_first(self):
        result = self.create(Principal("example-reader", "mcp", frozenset({Capability.READ})))

        self.assertEqual(result["error"]["code"], "forbidden")
