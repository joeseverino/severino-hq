from django.test import SimpleTestCase

from hq.platform.application.security import AuthorizationError, Capability, Principal


class HeldByNameTests(SimpleTestCase):
    """A grant held as a capability or as its name is the same grant, however it is asked for."""

    def test_a_grant_answers_by_its_member_and_by_its_name(self):
        for held in (Capability.READ_AUDIT_LOG, "read_audit_log"):
            principal = Principal("example", "web", frozenset({held}))
            with self.subTest(held=held):
                self.assertTrue(principal.permits(Capability.READ_AUDIT_LOG))
                self.assertTrue(principal.permits("read_audit_log"))

    def test_a_grant_not_held_is_refused_by_its_member_and_by_its_name(self):
        principal = Principal("example", "web", frozenset({Capability.READ}))

        for asked in (Capability.READ_AUDIT_LOG, "read_audit_log"):
            with self.subTest(asked=asked), self.assertRaises(AuthorizationError):
                principal.require(asked)
