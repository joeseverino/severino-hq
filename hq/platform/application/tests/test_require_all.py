from django.test import SimpleTestCase

from ..security import AuthorizationError, Capability, Principal, require_all

READER = Principal("reader", "test", frozenset({Capability.READ}))


class RequireAllTests(SimpleTestCase):
    def test_every_capability_held_passes(self):
        require_all(READER, (Capability.READ,))
        require_all(READER, ())

    def test_one_capability_missing_is_refused(self):
        with self.assertRaises(AuthorizationError):
            require_all(READER, (Capability.READ, Capability.MANAGE_INFRASTRUCTURE))
