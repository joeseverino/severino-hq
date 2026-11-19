"""Tokens only one module makes, which only that module's unsealer reads.

A token stands for a decision its module made, handed to a writer that must not
act without it: the sealer/unsealer pair of the E language's brands (Morris
1973; Miller 2006). The box (``Sealed``) is empty and cannot be called, copied
or pickled, and ``unseal`` refuses anything ``seal`` did not make. Out of
scope: code that reads a closure's cells or patches a module.
"""

from collections.abc import Callable
from typing import Never, Self, override
from weakref import WeakKeyDictionary


class Sealed:
    """An empty box that only its sealer makes and only its unsealer reads.

    A subclass declares ``__slots__ = ()``, so a box never grows a field.
    """

    __slots__ = ("__weakref__",)

    def __init_subclass__(cls) -> None:
        if cls.__dict__.get("__slots__") != ():
            raise TypeError(f"{cls.__name__} carries nothing of its own: declare __slots__ = ().")

    def __new__(cls) -> Self:
        raise TypeError(f"Only its sealer makes a {cls.__name__}.")

    @override
    def __reduce_ex__(self, protocol: object) -> Never:
        raise TypeError(f"A {type(self).__name__} is not copied or pickled.")

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


def sealer[Box: Sealed, Contents](
    box: type[Box], contents: type[Contents]
) -> tuple[Callable[[Contents], Box], Callable[[Box], Contents]]:
    """A new ``(seal, unseal)`` pair for ``box``, which carries a ``contents``.

    Each call makes a pair of its own, so a box another pair sealed is refused
    by this ``unseal``.
    """

    held: WeakKeyDictionary[Box, Contents] = WeakKeyDictionary()

    del contents

    def seal(carried: Contents) -> Box:
        token = object.__new__(box)
        held[token] = carried
        return token

    def unseal(token: Box) -> Contents:
        # The exact type first, so the lookup compares by identity: a
        # stand-in's own __eq__ and __hash__ never take part in it.
        if type(token) is not box or token not in held:
            raise TypeError(f"Not a {box.__name__} its sealer made.")
        return held[token]

    return seal, unseal
