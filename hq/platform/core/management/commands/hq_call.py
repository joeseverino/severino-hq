"""Call one MCP tool as the operator, from a shell on the host.

The shell already requires the operator's own credentials, so there is no
shared token; agents use MCP, each under its own identity. The contract is the
MCP tool surface itself, so the two cannot drift: ``{"tool": "<name>",
"arguments": {...}}`` on stdin, the tool's JSON result as an MCP caller receives
it on stdout. A refusal is the doors' ``{"ok": false}`` answer on stdout, its
message on stderr, and a non-zero exit.
"""

import json
import sys
from typing import Any, override

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from hq.platform.application.capabilities import adapter_refusal
from hq.platform.application.refusal_status import refusal_of, refuse
from hq.platform.application.security import cli_principal
from hq.platform.core import strict_json
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp

FIELDS = frozenset({"tool", "arguments"})


def answered(text: str) -> Any:
    """The answer to the request ``text`` holds: the tool's result, or the refusal a door gives."""

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

    principal = cli_principal()
    with set_principal(principal):
        try:
            return async_to_sync(tool.call)(arguments)
        except Exception as exc:  # noqa: BLE001 - a tool's failure is its answer, as at the doors
            return adapter_refusal(tool.name, exc, principal=principal)


class Command(BaseCommand):
    help = "Call one MCP tool as the operator: JSON request on stdin, JSON result on stdout."

    @override
    def handle(self, *args, **options):
        answer = answered(sys.stdin.read())
        self.stdout.write(json.dumps(answer, separators=(",", ":"), default=str))
        if isinstance(answer, dict) and answer.get("ok") is False:
            raise CommandError(refusal_of(answer).message)
