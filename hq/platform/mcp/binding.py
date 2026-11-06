"""Bind the declared MCP tools to their application adapters."""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from asgiref.sync import sync_to_async
from django.conf import settings
from pydantic import BaseModel, ValidationError

from hq.platform.application import agent_admission
from hq.platform.application.access import ANYONE
from hq.platform.application.refusal_status import refuse
from hq.platform.application.resources import name_refusal
from hq.platform.application.security import Principal

from .contract import served_resources
from .declarations import HANDLERS, argument_model, tool_contract
from .identity import caller_address, current_principal
from .shapes import Requirement, declared

logger = logging.getLogger("severino.mcp")

CALL_TIMED_OUT = "call_timed_out"


@dataclass(frozen=True, slots=True)
class Tool:
    """One MCP tool: what is advertised, and how a call reaches its handler."""

    name: str
    description: str
    # Generated from ``arguments``; the API document carries the same, and
    # `mise run lint` holds its written copy to it.
    input_schema: Mapping[str, Any]
    # The protocol's tool annotations: the behavior hints.
    annotations: Mapping[str, bool]
    arguments: type[BaseModel]
    handler: Callable[..., Any]
    # What a client shows a person for the tool.
    title: str = ""
    # What its structuredContent meets, success and refusal alike.
    output_schema: Mapping[str, Any] | None = None
    # Who may run it: a call is refused by it, and tools/list lists the tool
    # only to a caller it admits. A declared tool's is its declaration's; a
    # probe's own tool is anyone's.
    requires: Requirement = ANYONE
    execute: Callable[..., Any] = field(init=False)

    def __post_init__(self) -> None:
        run = partial(_held, self.handler, self.requires)
        object.__setattr__(self, "execute", sync_to_async(run, thread_sensitive=True))

    def offered_to(self, principal: Principal) -> bool:
        """Whether tools/list names this tool to ``principal``: the rule a call is refused by."""

        return self.requires.admits(principal)

    async def call(self, arguments: Mapping[str, Any]) -> Any:
        """The handler's answer, once the arguments pass its model, whose
        refusal never quotes the input, or the refusal for one that ran too long."""

        try:
            bound = self.arguments.model_validate(dict(arguments))
        except ValidationError as exc:
            # A resource tool's ``name`` is a choice in its schema, so a name it
            # cannot read fails here, before the read: it is refused as the
            # machine API refuses it, with the read that does answer.
            named = any(tuple(error["loc"]) == ("name",) for error in exc.errors())
            if named and (refused := name_refusal(self.name, arguments.get("name"))) is not None:
                raise refused from None
            raise
        seconds = settings.SEVERINO_AGENT_CALL_TIMEOUT_SECONDS
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                return await self.execute(**{name: getattr(bound, name) for name in type(bound).model_fields})
        except TimeoutError:
            if not deadline.expired():
                raise
        logger.warning(
            "MCP tool %s ran past %s seconds; answered, and its thread runs on",
            self.name,
            seconds,
            extra={"event": "mcp.call.timed_out", "tool": self.name, "actor": current_principal().actor},
        )
        return refuse(
            CALL_TIMED_OUT,
            f"HQ stopped waiting for this call after {seconds} seconds. It may still finish: read "
            "before repeating it, and repeat a change with the same idempotency_key.",
        )


def _held(handler: Callable[..., Any], requires: Requirement, **arguments: Any) -> Any:
    """``handler``'s answer, for a caller ``requires`` admits, run while it
    holds one of its agent's calls at once, on the request's thread.

    The call is given back when the handler returns, so one that outlived its
    timeout still counts against its agent until it ends.
    """

    principal = current_principal()
    requires.check(principal)
    with agent_admission.at_once(principal, source=caller_address()) as busy:
        if busy is not None:
            return busy.refusal.answer()
        return handler(**arguments)


def bind_tools() -> dict[str, Tool]:
    """One tool for each declared handler, as ``tool_contract`` states it."""

    resources = served_resources()
    entries = tool_contract()
    tools: dict[str, Tool] = {}
    for handler in HANDLERS:
        name = handler.__name__
        entry = entries[name]
        tools[name] = Tool(
            name,
            entry["description"],
            entry["inputSchema"],
            frozendict(entry["annotations"]),
            argument_model(handler, resources),
            handler,
            title=entry["title"],
            output_schema=entry["outputSchema"],
            requires=declared(handler).requires,
        )
    return tools
