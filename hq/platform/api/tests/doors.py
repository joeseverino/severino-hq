"""Every door a caller reaches HQ through, each driven by its own client, so a
promise HQ makes at all of them is one test run at each.

A call is named in the MCP tool vocabulary, which ``hq_call`` already speaks
(``execute_capability``, ``get_resource`` and the rest, with their
arguments), and each door turns it into its own request: the machine API an
HTTP request with a bearer token, MCP a tool call through the SDK's client,
the operator's shell ``hq_call``'s answer, an extension the SDK's
``execute_capability``. Each answers as an MCP caller reads it: a success's
fields beside ``ok``, a refusal ``{"ok": false, "error": ...}``.

A test calls ``at_the_door(self)`` in its ``setUp``, loops ``for door in
each_door(self, caller)``, and runs its promise inside ``with door:``.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from threading import local
from typing import Any, override
from unittest import TestCase
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from django.db import transaction
from django.test import Client, override_settings
from mcp import Client as MCPClient

from hq.platform.application import tokens
from hq.platform.application.security import MCP_INTERFACE, Principal, cli_principal
from hq.platform.core.management.commands.hq_call import answered
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp
from hq_sdk.capabilities import execute_capability as sdk_execute

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

# The identity provider the API's tokens are verified against.
AT_THE_DOOR = {
    "OIDC_ISSUER": ISSUER,
    "SEVERINO_API_RESOURCE": RESOURCE,
    "SEVERINO_API_LEEWAY_SECONDS": 30,
    "OIDC_RP_SIGN_ALGO": "RS256",
}
CAPABILITY = "execute_capability"


@dataclass(frozen=True)
class Caller:
    """An agent as its token names it: its client and the grant its scope carries."""

    client: str
    scope: str

    def principal(self) -> Principal:
        claims = {"client_id": self.client, "sub": self.client, "scope": self.scope}
        return tokens.token_principal(claims, MCP_INTERFACE)


# The person at the host's shell, who holds no token: only the shell and an extension act as them.
OPERATOR = None


def _query(arguments: Mapping[str, Any]) -> str:
    """Arguments as a query string carries them: text as it is, any other value as its JSON."""

    text = {name: value if isinstance(value, str) else json.dumps(value) for name, value in arguments.items()}
    return f"?{urlencode(text)}" if text else ""


class Door:
    name = ""

    def carries(self, caller: Caller | None, tool: str) -> bool:
        raise NotImplementedError

    def call(self, caller: Any, tool: str, arguments: Mapping[str, Any]) -> Any:
        raise NotImplementedError


class Api(Door):
    """The machine API, each response held to its OpenAPI document.

    ``documented=False`` sends changes and the capability catalog through a
    plain client, for a test whose capabilities (a supplied playbook's) the
    committed document does not list.
    """

    name = "api"

    def __init__(self, *, documented: bool = True) -> None:
        self.documented = documented
        # A client of each thread's own, for calls made at once.
        self._clients = local()

    def _client(self, tool: str) -> Client:
        plain = not self.documented and tool in {CAPABILITY, "describe_capabilities"}
        kind = "plain" if plain else "contract"
        if not hasattr(self._clients, kind):
            setattr(self._clients, kind, Client() if plain else ContractClient())
        client: Client = getattr(self._clients, kind)
        return client

    @override
    def carries(self, caller: Caller | None, tool: str) -> bool:
        return caller is not OPERATOR

    @override
    def call(self, caller: Any, tool: str, arguments: Mapping[str, Any]) -> Any:
        rest = dict(arguments)
        client = self._client(tool)
        headers = {"Authorization": f"Bearer {_token(caller.client, scope=caller.scope)}"}
        if tool == CAPABILITY:
            payload = dict(rest.pop("payload", {}))
            # The API's own place for a retry key is its header.
            if "idempotency_key" in payload:
                headers["Idempotency-Key"] = payload.pop("idempotency_key")
            response = client.post(
                f"/api/v2/capabilities/{rest.pop('name')}/",
                data=json.dumps({"payload": payload, **rest}),
                content_type="application/json",
                headers=headers,
            )
        else:
            response = client.get(_path(tool, rest), headers=headers)
        body = response.json()
        if body.get("ok") and isinstance(body.get("data"), dict):
            return {"ok": True, **body["data"]}
        return body


def _path(tool: str, arguments: dict[str, Any]) -> str:
    """The API path a read tool's call is, with what is left of its arguments as the query."""

    if tool == "get_resource":
        return f"/api/v2/resources/{arguments.pop('name')}/{arguments.pop('identifier')}/{_query(arguments)}"
    if tool == "list_resource":
        return f"/api/v2/resources/{arguments.pop('name')}/{_query(arguments)}"
    paths = {
        "describe_resources": "resources",
        "describe_capabilities": "capabilities",
        "get_topology": "topology",
        "get_findings": "findings",
    }
    return f"/api/v2/{paths[tool]}/{_query(arguments)}"


class Mcp(Door):
    """MCP, through the SDK's own client, declaring no extension."""

    name = "mcp"

    @override
    def carries(self, caller: Caller | None, tool: str) -> bool:
        return caller is not OPERATOR

    @override
    def call(self, caller: Any, tool: str, arguments: Mapping[str, Any]) -> Any:
        async def run() -> Any:
            async with MCPClient(mcp.server) as client:
                return await client.call_tool(tool, dict(arguments))

        with set_principal(caller.principal()):
            return async_to_sync(run)().structured_content


class Shell(Door):
    """``hq_call``, as the operator runs it on the host."""

    name = "hq_call"

    @override
    def carries(self, caller: Caller | None, tool: str) -> bool:
        return caller is OPERATOR

    @override
    def call(self, caller: Any, tool: str, arguments: Mapping[str, Any]) -> Any:
        return answered(json.dumps({"tool": tool, "arguments": dict(arguments)}))


class Sdk(Door):
    """An extension running a capability as the caller it acts for."""

    name = "hq_sdk"

    @override
    def carries(self, caller: Caller | None, tool: str) -> bool:
        return tool == CAPABILITY

    @override
    def call(self, caller: Any, tool: str, arguments: Mapping[str, Any]) -> Any:
        rest = dict(arguments)
        principal = cli_principal() if caller is OPERATOR else caller.principal()
        return sdk_execute(rest.pop("name"), rest.pop("payload", {}), principal=principal, **rest)


API, MCP, SHELL, SDK = Api(), Mcp(), Shell(), Sdk()
DOORS: tuple[Door, ...] = (API, MCP, SHELL, SDK)


class At:
    """One door as one caller asks through it. Entered, it is a subtest named
    for the door, and what the door wrote is rolled back on leaving."""

    def __init__(self, case: TestCase, door: Door, caller: Caller | None) -> None:
        self.case, self.door, self.caller = case, door, caller
        self._entered: AbstractContextManager[At] | None = None

    @property
    def name(self) -> str:
        return self.door.name

    def __call__(self, tool: str, **arguments: Any) -> Any:
        return self.door.call(self.caller, tool, arguments)

    def capability(self, name: str, payload: Mapping[str, Any], **carried: Any) -> Any:
        return self(CAPABILITY, name=name, payload=dict(payload), **carried)

    def as_(self, caller: Caller) -> At:
        """The same door, asked through by another caller."""

        return At(self.case, self.door, caller)

    @contextmanager
    def _within(self) -> Iterator[At]:
        with self.case.subTest(door=self.name), transaction.atomic():
            yield self
            transaction.set_rollback(True)

    def __enter__(self) -> At:
        self._entered = self._within()
        return self._entered.__enter__()

    def __exit__(self, *raised: Any) -> bool | None:
        assert self._entered is not None
        return self._entered.__exit__(*raised)


def at_the_door(case: TestCase) -> None:
    """The identity provider the API's tokens are verified against, for the rest of ``case``."""

    case.enterContext(override_settings(**AT_THE_DOOR))
    case.enterContext(_serving())


def each_door(case: TestCase, caller: Caller | None, *tools: str, doors: tuple[Door, ...] = DOORS) -> Iterator[At]:
    """Each of ``doors`` that carries ``caller`` and every one of ``tools`` (a capability's call by default)."""

    for door in doors:
        if all(door.carries(caller, tool) for tool in tools or (CAPABILITY,)):
            yield At(case, door, caller)
