"""A sealed token is made only by its seal and read only by its unsealer."""

import copy
import pickle
from typing import final

from django.test import SimpleTestCase

from ..sealing import Sealed, sealer


@final
class Example(Sealed):
    __slots__ = ()


SEAL, UNSEAL = sealer(Example, str)


class SealingTests(SimpleTestCase):
    def test_the_unsealer_reads_what_its_seal_made(self):
        self.assertEqual(UNSEAL(SEAL("carried")), "carried")

    def test_a_token_is_made_only_by_its_seal(self):
        with self.assertRaises(TypeError):
            Example()
        token = SEAL("carried")
        made_again = {
            "copy": copy.copy,
            "deepcopy": copy.deepcopy,
            "pickle": lambda token: pickle.loads(pickle.dumps(token)),
        }
        for way, again in made_again.items():
            with self.subTest(way=way), self.assertRaises(TypeError):
                again(token)

    def test_the_unsealer_refuses_what_its_seal_did_not_make(self):
        class Matching:
            """Equal to every token, and hashed as the one it stands beside."""

            def __eq__(self, other: object) -> bool:
                return True

            def __hash__(self) -> int:
                return hash(genuine)

        genuine = SEAL("carried")
        other_seal, _other_unseal = sealer(Example, str)
        refused = {
            "bare": object.__new__(Example),
            "another seal's": other_seal("carried"),
            "matching": Matching(),
            "unhashable": [],
        }
        for way, token in refused.items():
            with self.subTest(way=way), self.assertRaises(TypeError):
                UNSEAL(token)  # type: ignore[arg-type]

    def test_a_token_carries_nothing_of_its_own(self):
        token = SEAL("carried")
        with self.assertRaises(AttributeError):
            token.carried = "else"  # type: ignore[attr-defined]
        with self.assertRaises(TypeError):
            type("Fielded", (Sealed,), {"__slots__": ("carried",)})
