"""Call one MCP tool as the operator, from a shell on the host.

The operator reaches HQ's tools through a shell on the host, which already
requires their own credentials; there is no shared token. Agents use MCP, each
under its own identity.

The contract is the MCP tool surface itself, so the two cannot drift:

    stdin   {"tool": "<name>", "arguments": {...}}
    stdout  the tool's JSON result, exactly as an MCP caller receives it

A tool that fails writes its message to stderr and exits non-zero.
"""

from __future__ import annotations

import json
import sys

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand, CommandError

from application.security import cli_principal
from hq_mcp.identity import reset_principal, set_principal
from hq_mcp.server import mcp


class Command(BaseCommand):
    help = "Call one MCP tool as the operator: JSON request on stdin, JSON result on stdout."

    def handle(self, *args, **options):
        try:
            request = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise CommandError(f"The request is not JSON: {exc}") from exc
        if not isinstance(request, dict) or not isinstance(request.get("tool"), str):
            raise CommandError('The request needs a string "tool".')
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise CommandError('"arguments" must be a JSON object.')
        tool = mcp._tool_manager.get_tool(request["tool"])
        if tool is None:
            raise CommandError(f"There is no tool named {request['tool']!r}.")

        bound = set_principal(cli_principal())
        try:
            result = async_to_sync(tool.run)(arguments)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise CommandError(str(exc)) from exc
        finally:
            reset_principal(bound)
        self.stdout.write(json.dumps(result, separators=(",", ":"), default=str))
