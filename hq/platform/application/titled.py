"""An operation HQ binds by a name no capability, job or controller action
declares, carrying what a person calls it.

A leaf, importing nothing, so the module that binds the operation declares
its title beside its name.
"""

from typing import Self


class Titled(str):
    """An operation's name, as audit rows store and bindings take it, with its title."""

    title: str

    def __new__(cls, name: str, title: str) -> Self:
        named = super().__new__(cls, name)
        named.title = title
        return named
