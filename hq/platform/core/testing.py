"""What a test arranges most: a person, signed in or not, a declared resource,
a provider connection, the principals a use case is called as, a template
drawn and the widths a table's columns leave at, each made one way here."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.template import Context
from django.test import Client, TestCase
from django.utils import timezone

from hq.domains.control_plane.connection_kinds import CONNECTION_CREDENTIALS
from hq.domains.control_plane.models import InboundSource, ManagedResource, ProviderConnection
from hq.platform.application import readings, roles
from hq.platform.application.inbound_events import CHANNEL_EVERY, channel
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
    "channel_heard",
    "connection",
    "declare",
    "leaving",
    "managing_everything",
    "operator",
    "person",
    "render",
    "run_ended",
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


def connection(
    provider: str, connection_ref: str = "", /, *, controller_id: str = "example-controller", **fields: Any
) -> ProviderConnection:
    """A connection of ``provider`` the example controller reported now, with
    ``fields`` for any other column a test is about."""

    found, _ = ProviderConnection.objects.update_or_create(
        connection_ref=connection_ref or f"example-{provider}",
        controller_id=controller_id,
        defaults={"provider": provider, "observed_at": timezone.now(), **fields},
    )
    return found


def managing_everything(*refs: tuple[str, str]) -> None:
    """A managing connection per provider, plus each named ``(provider, ref)``."""

    for provider in CONNECTION_CREDENTIALS:
        connection(provider, manages=True)
    for provider, ref in refs:
        connection(provider, ref, manages=True)


def channel_heard(source: InboundSource = InboundSource.WORKER, *, silent: bool = False) -> None:
    """``source``'s event channel last heard a moment ago, so its events keep
    current what they cover; or, ``silent``, long enough ago that they keep
    nothing and a page opened asks for it again."""

    observed_at = timezone.now() - (3 * CHANNEL_EVERY if silent else timedelta(0))
    readings.record(readings.heartbeat(channel(source)), {}, observed_at=observed_at)


def run_ended(operation_id: str, worker: str, *, events: Any = (), ending: str = "succeeded", **report: Any) -> None:
    """A claimed run's worker saying its tasks, as its renewals do, then how it
    ended (``ControllerReport``), as ``ending`` with the ``RunOutcome`` fields given."""

    from hq.domains.control_plane.models import OperationRequest
    from hq.platform.application.controller import ControllerReport, renew_operation, report_operation
    from hq.platform.application.run_outcome import Ending, ended_as

    if events:
        renew_operation(str(operation_id), controller_id=worker, events=list(events))
    generation = OperationRequest.objects.get(pk=operation_id).input["generation"]
    said = {field: report.pop(field) for field in ("task", "host", "message") if field in report}
    outcome = ended_as(Ending(ending), **said)
    report_operation(
        str(operation_id),
        ControllerReport(success=ending == Ending.SUCCEEDED, observed_generation=generation, outcome=outcome, **report),
        controller_id=worker,
    )


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
