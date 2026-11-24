"""What a test arranges most: a person, signed in or not, a declared resource,
the principals a use case is called as, a template drawn and the widths a
table's columns leave at, each made one way here."""

from dataclasses import replace
from typing import Any

from django.contrib.auth import get_user_model
from django.template import Context
from django.test import Client, TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application import roles
from hq.platform.application.markup_testing import elements
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tables import Cell, DataTable, Row, TableColumn
from hq.platform.core import held_values

from .test_runner import no_default_role

__all__ = (
    "AGENT",
    "MANAGER",
    "MCP_READER",
    "NOBODY",
    "OPERATOR",
    "READER",
    "SignedInTestCase",
    "declare",
    "leaving",
    "operator",
    "person",
    "render",
)

# A reader, a manager of infrastructure, everything and nothing, as a test calls
# a use case; and the agents an MCP test calls as.
READER = Principal("reader", "test", frozenset({Capability.READ}))
MANAGER = Principal("operator", "test", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
OPERATOR = Principal("operator", "test", frozenset(Capability))
NOBODY = Principal("nobody", "test", frozenset())
MCP_READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


def operator(client: Client | None = None, /, username: str = "operator", **fields: Any) -> Any:
    """An account holding the operator role, signed in to ``client`` when one is given."""

    user = get_user_model().objects.create_user(username, **fields)
    roles.give_operator(user)
    return _signed_in(client, user)


def person(client: Client | None = None, /, username: str = "person", **fields: Any) -> Any:
    """An account holding no role, signed in to ``client`` when one is given."""

    with no_default_role():
        user = get_user_model().objects.create_user(username, **fields)
    return _signed_in(client, user)


def _signed_in(client: Client | None, user: Any) -> Any:
    if client is not None:
        client.force_login(user)
    return user


def declare(key: str, kind: str, spec: dict[str, Any] | None = None, /, **fields: Any) -> ManagedResource:
    """A resource HQ declares, with ``fields`` for any other column a test is about."""

    return ManagedResource.objects.create(key=key, kind=kind, spec=spec or {}, **fields)


def render(source: str, **context: Any) -> str:
    """A template written in the test, drawn with ``context``."""

    return held_values.template(source).render(Context(context))


def leaving(*columns: TableColumn) -> dict[str, int]:
    """The level each column leaves a narrow box at (`data-drop`), by its
    label, 0 for one it keeps, in a table of one row of values, sorted on
    the page since a server's sort is no part of it."""

    drawn = tuple(replace(column, sort="") for column in columns)
    table = DataTable("Things", drawn, (Row(tuple(Cell("1") for _ in columns)),))
    headings = elements(render("{% data_table table %}", table=table), "th")
    return {column.label: int(th.attrs.get("data-drop") or 0) for column, th in zip(columns, headings, strict=True)}


class SignedInTestCase(TestCase):
    """A test case whose client is signed in as ``self.user``, an operator."""

    user: Any

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.user = operator()

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
