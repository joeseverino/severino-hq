from dataclasses import dataclass

from django.test import SimpleTestCase

from hq.platform.core.testing import READER

from ..security import AuthorizationError, Capability, Requires


@dataclass(frozen=True, slots=True)
class Spec(Requires):
    required_capability: Capability | str | tuple[Capability | str, ...]


class RequiresTests(SimpleTestCase):
    def test_a_principal_holding_every_capability_is_permitted(self):
        for spec in (Spec(Capability.READ), Spec((Capability.READ,)), Spec(())):
            with self.subTest(spec=spec):
                spec.authorize(READER)
                self.assertTrue(spec.permitted_to(READER))

    def test_a_principal_lacking_one_capability_is_refused(self):
        spec = Spec((Capability.READ, Capability.MANAGE_INFRASTRUCTURE))
        with self.assertRaises(AuthorizationError):
            spec.authorize(READER)
        self.assertFalse(spec.permitted_to(READER))

    def test_require_takes_a_set(self):
        READER.require(Capability.READ)
        READER.require()
        with self.assertRaises(AuthorizationError):
            READER.require(Capability.READ, Capability.MANAGE_INFRASTRUCTURE)
