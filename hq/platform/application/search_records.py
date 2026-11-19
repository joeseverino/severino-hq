"""The shape the search resource answers in: one match."""

from typing import Annotated, TypedDict

from .answer_fields import PageUrl
from .describe import Describe


class Match(TypedDict):
    """One search match: a machine, service or domain, or a record."""

    kind: Annotated[
        str, Describe("What sort of match it is, such as `machine`, `service` or `zone`, or the list a record is in.")
    ]
    name: Annotated[str, Describe("Its id where it is listed, such as a machine's name, a hostname or a record's key.")]
    label: Annotated[str, Describe("What it is called, as a result shows it.")]
    summary: Annotated[str, Describe("A line on it, with the text that matched.")]
    url: PageUrl
    group: Annotated[str, Describe("The heading a list of results files it under.")]
