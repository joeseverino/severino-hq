"""The prompts HQ offers a person through an agent's client, which Claude Code lists as ``/hq:`` commands.

Each is one message: what to make of it, and HQ's own answer to read, built
from the same projections the tools and resources read, as the caller, so it
holds what this caller may read and names nothing HQ does not.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp import types
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter

from hq.platform.application import read_models
from hq.platform.application.findings import findings
from hq.platform.application.security import Principal

from .resources import action_queue, held_requests, hq_itself

_JSON = TypeAdapter(Any)
# The open problems a brief leads with, most severe first; the summary still counts every one.
BRIEF_FINDINGS = 10


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    title: str
    description: str
    ask: str
    read: Callable[[Principal], Mapping[str, Any]]


def _status(principal: Principal) -> dict[str, Any]:
    problems = findings(principal=principal, severity="attention", limit=BRIEF_FINDINGS)
    return {
        "hq": hq_itself(principal),
        "health": read_models.system_health(principal=principal),
        "problems": {"summary": problems["summary"], "findings": problems["findings"]},
    }


PROMPTS = {
    prompt.name: prompt
    for prompt in (
        Prompt(
            "needs-me",
            "What needs me",
            "What HQ's action queue holds for you, most urgent first.",
            "Tell me what needs me in HQ, most urgent first: for each, what it is and the one thing to do, "
            "with HQ's page for it. HQ's action queue:",
            action_queue,
        ),
        Prompt(
            "status-brief",
            "Status brief",
            "HQ itself, its health and its open problems, in a few lines.",
            "Give me a short status brief of HQ: the release it runs, its health, and the open problems "
            "that matter, most severe first, each with its remedy. What HQ reports:",
            _status,
        ),
        Prompt(
            "held-requests",
            "Review held requests",
            "Each change you asked for that waits for a person to decide.",
            "Walk me through each change you asked for that waits for my decision in HQ: what it would change "
            "and what to weigh, with its review page. Only I decide them, in HQ. The held requests:",
            held_requests,
        ),
    )
}


def listed() -> types.ListPromptsResult:
    return types.ListPromptsResult(
        prompts=[
            types.Prompt(name=prompt.name, title=prompt.title, description=prompt.description)
            for prompt in PROMPTS.values()
        ]
    )


def message(name: str, principal: Principal) -> types.GetPromptResult:
    """The prompt's one message, read now as ``principal``."""

    prompt = PROMPTS.get(name)
    if prompt is None:
        raise MCPError(
            types.INVALID_PARAMS,
            f"HQ has no prompt named {name!r}. prompts/list names each one.",
            data={"next": {"method": "prompts/list"}},
        )
    said = _JSON.dump_json(prompt.read(principal), by_alias=True, fallback=str).decode()
    return types.GetPromptResult(
        description=prompt.description,
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(type="text", text=f"{prompt.ask}\n{said}"))
        ],
    )
