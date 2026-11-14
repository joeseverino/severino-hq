"""Call one MCP tool as the operator, from a shell on the host.

The shell already requires the operator's own credentials, so there is no
shared token; agents use MCP, each under its own identity. The contract is the
MCP tool surface itself, so the two cannot drift: ``{"tool": "<name>",
"arguments": {...}}`` on stdin, the tool's JSON result as an MCP caller receives
it on stdout. A refusal is the doors' ``{"ok": false}`` answer on stdout, its
message on stderr, and a non-zero exit.

``--agent <client> --grant <permission>`` makes the call as that agent instead,
on a development server, so an agent's path is walked without an identity
provider. Its security properties:

- It answers only with DEBUG on and when every host Django serves is a
  loopback one, as a scratch server's are, and refuses otherwise, so it
  never acts on a server anyone else reaches.
- The principal is ``agent_principal``'s of the grant named, the one way a
  token becomes a principal, so it holds no permission the command line did
  not name and none an agent's token could not carry. A name HQ does not
  grant an agent is refused, never dropped.
- The call is let through as a verified token is (``agent_admission.admitted``)
  and served by the MCP server's own tool call, in an operation bound to the
  agent alone, so its rate, brakes, authorization and approval are the ones
  an agent meets over /mcp/.
"""

import json
import sys
from collections.abc import Iterable
from typing import Any, override

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
lazy from mcp import Client
lazy from mcp.shared.exceptions import MCPError

from hq.platform.application.capabilities import adapter_refusal
from hq.platform.application.refusal_status import refusal_of, refuse
from hq.platform.application.security import Principal, cli_principal
from hq.platform.core import strict_json
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp
lazy from hq.platform.application import agent_admission
lazy from hq.platform.application.reach import network_of
lazy from hq.platform.application.security import MCP_INTERFACE, agent_grantable, agent_principal
lazy from hq.platform.core.attribution import outside_every_door
lazy from hq.platform.core.audit import audit_operation

FIELDS = frozenset({"tool", "arguments"})
# Where the agent door records its calls from: the server's own loopback.
LOOPBACK = "127.0.0.1"


def _request(text: str) -> tuple[Any, dict[str, Any]] | dict[str, Any]:
    """The tool ``text`` names and its arguments, or the refusal a door gives."""

    try:
        request = strict_json.loads(text)
    except ValueError:
        return refuse("invalid_json", "The request is not valid JSON.")
    if not isinstance(request, dict):
        return refuse("invalid_json", "The request must be a JSON object.")
    unknown = request.keys() - FIELDS
    if unknown:
        return refuse("invalid_input", f"Unknown request fields: {', '.join(sorted(unknown))}.")
    if not isinstance(request.get("tool"), str):
        return refuse("invalid_input", 'The request needs a string "tool".')
    arguments = request.get("arguments", {})
    if not isinstance(arguments, dict):
        return refuse("invalid_input", '"arguments" must be a JSON object.')
    tool = mcp.tools.get(request["tool"])
    if tool is None:
        return refuse("invalid_input", f"Unknown tool: {request['tool']}")
    return tool, arguments


def answered(text: str) -> Any:
    """The answer to the request ``text`` holds: the tool's result, or the refusal a door gives."""

    asked = _request(text)
    if isinstance(asked, dict):
        return asked
    tool, arguments = asked
    principal = cli_principal()
    with set_principal(principal):
        try:
            return async_to_sync(tool.call)(arguments)
        except Exception as exc:  # noqa: BLE001 - a tool's failure is its answer, as at the doors
            return adapter_refusal(tool.name, exc, principal=principal)


def agent_door(client: str, grant: Iterable[str]) -> Principal:
    """The agent ``client`` holding ``grant``, or the reason this server has no agent door."""

    if not settings.DEBUG:
        raise CommandError("hq_call --agent runs only with DEBUG on: it is for a development server.")
    served = [host for host in settings.ALLOWED_HOSTS if not _loopback(host)]
    if served or not settings.ALLOWED_HOSTS:
        raise CommandError(
            "hq_call --agent runs only on a server loopback alone reaches, as dev:scratch's is; "
            f"this one serves {', '.join(served) or 'any host'}."
        )
    client = client.strip()
    if not client:
        raise CommandError("--agent needs the agent's client id.")
    names = frozenset(name.strip() for each in grant for name in each.split(",") if name.strip())
    if not names:
        raise CommandError("--agent needs --grant, the permissions its token would carry.")
    unknown = names - agent_grantable()
    if unknown:
        raise CommandError(
            f"No agent's token can carry {', '.join(sorted(unknown))}. "
            f"It may name: {', '.join(sorted(agent_grantable()))}."
        )
    return agent_principal(client, MCP_INTERFACE, names)


def _loopback(host: str) -> bool:
    name = host.strip().lower().removeprefix("[").removesuffix("]")
    return name == "localhost" or network_of(name) == "loopback"


def answered_as(text: str, principal: Principal) -> Any:
    """The answer to the request ``text`` holds, made by the agent ``principal``
    through the MCP server's own tool call."""

    asked = _request(text)
    if isinstance(asked, dict):
        return asked
    tool, arguments = asked

    async def call() -> Any:
        async with Client(mcp.server) as client:
            return await client.call_tool(tool.name, arguments)

    # Admitted and bound as the MCP door admits and binds a caller, outside the
    # shell's own operation, whose rows are the operator's.
    with outside_every_door():
        admitted = agent_admission.admitted(principal, source=LOOPBACK)
    with (
        outside_every_door(),
        agent_admission.carrying(admitted) as caller,
        set_principal(caller),
        audit_operation(operation=MCP_INTERFACE, principal=caller),
    ):
        try:
            result = async_to_sync(call)()
        except MCPError as exc:
            return refuse("invalid_input", str(exc))
    return result.structured_content


class Command(BaseCommand):
    help = "Call one MCP tool as the operator, or as an agent on a development server: JSON request on stdin, JSON result on stdout."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--agent", help="Call as this agent's client id; DEBUG and loopback only.")
        parser.add_argument(
            "--grant",
            action="append",
            default=[],
            help="A permission the agent holds, or several separated by commas; repeatable.",
        )

    @override
    def handle(self, *args, **options):
        if options["agent"] is None and options["grant"]:
            raise CommandError("--grant names what an agent holds, so it needs --agent.")
        if options["agent"] is None:
            answer = answered(sys.stdin.read())
        else:
            principal = agent_door(options["agent"], options["grant"])
            answer = answered_as(sys.stdin.read(), principal)
        self.stdout.write(json.dumps(answer, separators=(",", ":"), default=str))
        if isinstance(answer, dict) and answer.get("ok") is False:
            raise CommandError(refusal_of(answer).message)
