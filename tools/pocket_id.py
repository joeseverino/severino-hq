"""Write Pocket ID's permission list for HQ's API from HQ's catalog, and grant a client every one of them.

The task `wire-pocket-id` runs it on the operator's machine (docs/API.md,
Setting up a client). The admin key travels as a secret does in
tools/credentials.py: from the environment, in one header, never an argument,
the output or a message. HQ stores no admin key, since one at Pocket ID
v2.19.2 carries no scopes and so could do anything.
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any
from urllib.parse import urlencode

from django.utils.text import capfirst
lazy import django

from hq.platform.core.tests.task import Usage
from tools.credentials import Call, Refused, Send, UsageError, https

# Each loads once django.setup() has run, in ``catalog``.
lazy from hq.platform.application.integrations import integration_graph
lazy from hq.platform.application.tokens import DEVICE_SCOPE
lazy from hq.platform.mcp.security import token_scopes

KEY_VARIABLE = "POCKET_ID_API_KEY"
# What Pocket ID v2.19.2 takes for a permission (backend/internal/api/dto.go,
# apiPermissionInputDto), and for a client id (client_id_param.go).
KEY_LIMIT, NAME_LIMIT, DESCRIPTION_LIMIT = 128, 50, 200
_CLIENT = re.compile(r"^[A-Za-z0-9._-]+$")
# The permission that marks a client as a person's own device. Written so the
# replacing PUT keeps it, and granted by an administrator per client, never here.
DEVICE_DESCRIPTION = "Marks a client as a person's own device, which stands as that person"


@dataclass(frozen=True, slots=True)
class Permission:
    key: str
    name: str
    description: str = ""

    def body(self) -> dict[str, str]:
        written = {"key": self.key, "name": self.name}
        return written | {"description": self.description} if self.description else written


def in_words(key: str) -> str:
    """``manage_infrastructure`` as "Manage infrastructure"."""

    words = re.sub(r"[._]+", " ", key).strip()
    return capfirst(words)


def described(titles: Sequence[str]) -> str:
    """Whole command titles while they fit Pocket ID's limit, then how many more: never half a word."""

    for shown in range(len(titles), -1, -1):
        rest = len(titles) - shown
        parts = [*titles[:shown], *([f"and {rest} more"] if rest else [])]
        if shown == 0 and rest:
            parts = [f"{rest} commands"]
        text = "; ".join(parts)
        if len(text) <= DESCRIPTION_LIMIT:
            return text
    return ""


def listed(scopes: Sequence[str], commands: Mapping[str, Sequence[str]], device: str) -> tuple[Permission, ...]:
    """The token scopes, each named in words and described by the commands it permits, and the device permission.

    A key or name past Pocket ID's limit is refused, naming it, rather than cut.
    """

    permissions = (
        *(Permission(scope, in_words(scope), described(sorted(set(commands.get(scope, ()))))) for scope in scopes),
        Permission(device, in_words(device), DEVICE_DESCRIPTION),
    )
    long = [p.key for p in permissions if len(p.key) > KEY_LIMIT or len(p.name) > NAME_LIMIT]
    if long:
        raise Refused(f"Past Pocket ID's limits ({KEY_LIMIT} for a key, {NAME_LIMIT} for its name): {', '.join(long)}")
    return permissions


def catalog() -> tuple[Permission, ...]:
    """The list from the catalog this process composed: the host and the extensions it loads."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
    django.setup()

    commands: dict[str, list[str]] = defaultdict(list)
    for spec in integration_graph().capabilities.values():
        for needed in spec.required_capabilities:
            commands[str(needed)].append(spec.title)
    return listed(token_scopes(), commands, DEVICE_SCOPE)


# ------------------------------------------------------------------ the write


@dataclass(frozen=True, slots=True)
class Run:
    usage: Usage
    environ: Mapping[str, str]
    send: Send
    out: IO[str]
    err: IO[str]

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.err)

    def call(self, method: str, path: str, what: str, body: Mapping[str, Any] | None = None) -> Any:
        base = self.usage.text("pocket_id").rstrip("/")
        call = Call(method, f"{base}/api{path}", token=self.environ[KEY_VARIABLE], header="X-API-Key", json=body)
        answer = self.send(call)
        if isinstance(answer, dict) and "error" in answer:
            raise Refused(f"Pocket ID refused {what}: {answer['error']}")
        if answer is None:
            raise Refused(f"Pocket ID answered {what} with something other than JSON.")
        return answer


def check_grant_flags(client: str, machine: str, scopes: str) -> None:
    """Refuse a grant request that mixes the two modes or names an invalid client id."""

    if machine and client:
        raise UsageError("Choose --grant or --machine-grant, never both.")
    if bool(machine) != bool(scopes):
        raise UsageError("--machine-grant and --permissions are required together.")
    if machine and not _CLIENT.fullmatch(machine):
        raise UsageError("A Pocket ID client id is letters, digits, dots, hyphens and underscores.")
    if client and not _CLIENT.match(client):
        raise UsageError("A Pocket ID client id is letters, digits, dots, hyphens and underscores.")


def find_api(run: Run, resource: str) -> dict[str, Any]:
    """The one Pocket ID API whose resource is the one named."""

    found = run.call("GET", "/apis?" + urlencode({"search": resource, "pagination[limit]": 100}), "the API listing")
    apis = [api for api in found.get("data") or () if isinstance(api, dict) and api.get("resource") == resource]
    if len(apis) != 1:
        raise Refused(f"Pocket ID has no API whose resource is {resource}: create it first (docs/API.md).")
    return apis[0]


def write_catalog(run: Run, api: Mapping[str, Any], resource: str, permissions: Sequence[Permission]) -> dict[Any, Any]:
    """Replace the API's permission list and return the ids Pocket ID holds for it."""

    before = {p.get("key") for p in api.get("permissions") or ()}
    body = {"permissions": [permission.body() for permission in permissions]}
    written = run.call("PUT", f"/apis/{api['id']}/permissions", "the permission list", body)
    ids = {p.get("key"): p.get("id") for p in written.get("permissions") or ()}
    if set(ids) != {permission.key for permission in permissions}:
        raise Refused("Pocket ID's list after the write is not the one sent; read it under Administration, APIs.")
    run.say(f"Wrote {len(ids)} permissions to {api.get('name')} ({resource}).")
    if removed := sorted(str(key) for key in before - set(ids)):
        run.say(f"Removed: {', '.join(removed)}")
    return ids


def wire(run: Run, permissions: Sequence[Permission]) -> None:
    for permission in permissions:
        print(f"{permission.key}\t{permission.name}\t{permission.description}", file=run.out)
    client = run.usage.text("grant")
    machine = run.usage.text("machine_grant")
    scopes = run.usage.text("permissions")
    check_grant_flags(client, machine, scopes)
    if not run.environ.get(KEY_VARIABLE):
        run.say(f"Nothing changed. Set {KEY_VARIABLE} to a Pocket ID admin's API key (op run) to write this list.")
        return
    resource = run.usage.text("resource")
    if not run.usage.text("pocket_id") or not resource:
        raise UsageError("--pocket-id and --resource name the Pocket ID and the API to write to.")

    api = find_api(run, resource)
    if machine:
        machine_grant(run, machine, api, scopes)
        return
    ids = write_catalog(run, api, resource, permissions)
    if client:
        grant(run, client, api["id"], ids, permissions)


def machine_grant(run: Run, client: str, api: Mapping[str, Any], requested: str) -> None:
    """Replace one machine's grant using existing permission ids, without changing the API catalog."""

    scopes = requested.split(",")
    ids = {p["key"]: p["id"] for p in api.get("permissions") or ()}
    if not all(scopes) or len(scopes) != len(set(scopes)) or set(scopes) - set(ids):
        raise UsageError("--permissions must name distinct permissions already registered on this API.")
    if DEVICE_SCOPE in scopes:
        raise UsageError("A machine must never hold own_device.")
    held = run.call("GET", f"/api-access/{client}/apis", f"{client}'s grants")
    current = next((g for g in held if (g.get("api") or {}).get("id") == api["id"]), {})
    delegated = list(current.get("userDelegatedPermissionIds") or ())
    if ids.get(DEVICE_SCOPE) in delegated:
        raise Refused("This client is registered as a person's own device; use a dedicated agent client.")
    selected = [ids[scope] for scope in scopes]
    body = {
        "clientAccess": True,
        "clientPermissionIds": selected,
        "userDelegatedAccess": bool(current.get("userDelegatedAccess")),
        "userDelegatedPermissionIds": delegated,
    }
    applied = run.call("PUT", f"/apis/{api['id']}/clients/{client}", f"{client}'s machine grant", body)
    if not applied.get("clientAccess") or set(applied.get("clientPermissionIds") or ()) != set(selected):
        raise Refused("Pocket ID did not apply the exact machine grant; read it under Administration, APIs.")
    run.say(f"{client}'s machine grant is exactly: {', '.join(scopes)}. The API catalog was not changed.")


def grant(run: Run, client: str, api: str, ids: Mapping[str, str], permissions: Sequence[Permission]) -> None:
    """Every permission but the device's, delegated by a signed-in person; the rest of the client's grant kept."""

    held = run.call("GET", f"/api-access/{client}/apis", f"{client}'s grants")
    grants = held if isinstance(held, list) else []
    current = next((g for g in grants if isinstance(g, dict) and (g.get("api") or {}).get("id") == api), {})
    delegated = [ids[p.key] for p in permissions if p.key != DEVICE_SCOPE]
    # A device's own standing is an administrator's to give or take, so the grant leaves it as it was.
    if ids[DEVICE_SCOPE] in (current.get("userDelegatedPermissionIds") or ()):
        delegated.append(ids[DEVICE_SCOPE])
    body = {
        "userDelegatedAccess": True,
        "clientAccess": bool(current.get("clientAccess")),
        "userDelegatedPermissionIds": delegated,
        "clientPermissionIds": list(current.get("clientPermissionIds") or ()),
    }
    applied = run.call("PUT", f"/apis/{api}/clients/{client}", f"{client}'s grant", body)
    if set(delegated) - set(applied.get("userDelegatedPermissionIds") or ()):
        raise Refused(f"Pocket ID did not grant {client} every permission; read it under Administration, APIs.")
    run.say(f"{client} may ask for every permission but {DEVICE_SCOPE} on a signed-in person's behalf.")


def main(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        wire(Run(Usage.from_environment(environ), environ, https, sys.stdout, sys.stderr), catalog())
    except Refused as refused:
        print(refused, file=sys.stderr)
        return refused.status
    return 0


if __name__ == "__main__":
    sys.exit(main())
