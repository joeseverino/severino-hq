"""A sealed token is made only by its seal and read only by its unsealer."""

from typing import final

from django.test import SimpleTestCase

from ..sealing import Sealed, sealer


@final
class Example(Sealed):
    __slots__ = ()


SEAL, UNSEAL = sealer(Example, str)


class SealingTests(SimpleTestCase):
    def test_a_token_carries_nothing_of_its_own(self):
        token = SEAL("carried")
        with self.assertRaises(AttributeError):
            token.carried = "else"  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            type("Fielded", (Sealed,), {"__slots__": ("carried",)})
