"""The fields several record commands declare alike, each said once.

Each is a named type alias, so every schema that takes one (a command's,
the API document's) writes its definition once under the alias's name and
each use as a reference to it, with that use's default beside the reference.
A field only one record declares is declared there, where it means what it
says.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from .command_fields import Describe

type Notes = Annotated[str, Describe("Anything else worth keeping.")]
type Slug = Annotated[str, Describe("Its address in HQ; blank derives one from the name.")]
type TotalCost = Annotated[Decimal, Describe("What it cost in all.")]
type PublishedAt = Annotated[date | None, Describe("When it was published.")]
type PublishedUrl = Annotated[str, Describe("Where it is published.")]
