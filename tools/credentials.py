"""Mint HQ's read-only observer credentials, and wire its GitHub App, on the operator's machine.

The tasks `mint:cloudflare`, `mint:tailscale` and `wire-github-app` run it
(docs/Secrets.md, Minting observer credentials). A secret travels in memory and
on stdin only, and never reaches an argument list, a file, stdout or a message.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

import httpx2
import jwt

from hq.platform.application.http_read import UNREACHABLE
from hq.platform.core.tests.task import Usage
lazy from hq.domains.control_plane.credential_reads import observer_permissions
lazy from hq.domains.control_plane.provider_adapters.github import REVIEW_EVENTS
lazy from hq.platform.application.entity_links import https_url

# The secrets the wiring sets, as HQ's posture check names them.
lazy from hq.platform.application.github_posture import APP_CLIENT_ID, APP_KEY, REVIEW_APP_ID

STORED = "Stored in {item} ({vault}); the controller reads it on its next render."


class Refused(Exception):
    """A run that cannot go on; the message says why and holds no secret."""

    status = 1


class UsageError(Refused):
    status = 2


# ------------------------------------------------------------------- 1Password


@dataclass(frozen=True, slots=True)
class Store:
    """``op://<vault>/<item>/<field>``: a field named directly, not through a section."""

    vault: str
    item: str
    field: str

    @classmethod
    def parse(cls, reference: str) -> Store:
        parts = reference.removeprefix("op://").split("/") if reference.startswith("op://") else []
        if len(parts) > 3 and all(parts[:3]):
            raise UsageError("A store reference names a field directly, not a section.")
        if len(parts) != 3 or not all(parts):
            raise UsageError("A store reference is op://<vault>/<item>/<field>.")
        return cls(*parts)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class Programs:
    """``op`` and ``gh``. A value they need goes on stdin, never into the command."""

    runner: Runner = subprocess.run

    def run(self, command: Sequence[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            return self.runner(list(command), capture_output=True, text=True, check=False, **feed)
        except FileNotFoundError:
            raise Refused(f"{command[0]} is required and is not on PATH.") from None

    def output(self, command: Sequence[str], *, stdin: str | None = None) -> str:
        done = self.run(command, stdin=stdin)
        if done.returncode != 0:
            # The command's words only: stdin may be a secret.
            raise Refused(f"{' '.join(command[:3])} failed: {done.stderr.strip()}")
        return done.stdout


def _named(entry: Mapping[str, Any], name: str) -> bool:
    return name in (entry.get("id"), entry.get("label"))


@dataclass(frozen=True, slots=True)
class OnePassword:
    """An existing item, read as JSON and edited through its template on stdin."""

    programs: Programs = field(default_factory=Programs)

    def _item(self, vault: str, item: str, *, reveal: bool = False) -> dict[str, Any]:
        command = ["op", "item", "get", item, "--vault", vault, "--format", "json"]
        try:
            found = json.loads(self.programs.output(command + ["--reveal"] * reveal))
        except ValueError:
            found = None
        if not isinstance(found, dict):
            raise Refused(f"op returned no item {item} in {vault}.")
        return found

    def check(self, stores: Sequence[Store]) -> None:
        """Each field once on the item, before anything is created for it."""

        vault, item = stores[0].vault, stores[0].item
        fields = self._item(vault, item).get("fields") or []
        names = [store.field for store in stores]
        if not all(sum(_named(entry, name) for entry in fields) == 1 for name in names):
            raise Refused(f"The item {item} in {vault} does not carry each of: {' '.join(names)}.")

    def write(self, stores: Sequence[Store], values: Sequence[str]) -> None:
        vault, item = stores[0].vault, stores[0].item
        found = self._item(vault, item, reveal=True)
        wanted = dict(zip((store.field for store in stores), values, strict=True))
        for entry in found.get("fields") or []:
            for name, value in wanted.items():
                if _named(entry, name):
                    entry["value"] = value
        self.programs.output(["op", "item", "edit", item, "--vault", vault], stdin=json.dumps(found))


# ------------------------------------------------------------------------ HTTP


@dataclass(frozen=True, slots=True)
class Call:
    """One API request. The token goes in a header, never the URL: ``Authorization``
    as a bearer token, or as the whole value of the ``header`` an API names."""

    method: str
    url: str
    token: str = field(default="", repr=False)
    header: str = "Authorization"
    json: Mapping[str, Any] | None = None
    form: Mapping[str, str] | None = field(default=None, repr=False)


Send = Callable[[Call], Any]


def https(call: Call) -> Any:
    """The answer's JSON, whatever the status: each API says in it why it refused. None when it is not JSON.

    A redirect is an answer too, never a second request carrying the header
    elsewhere: httpx2 follows one only when asked.
    """

    if not https_url(call.url):
        raise UsageError("The API base must be an https:// address.")
    bearer = call.header == "Authorization"
    headers = {call.header: f"Bearer {call.token}" if bearer else call.token} if call.token else {}
    try:
        answer = httpx2.request(call.method, call.url, headers=headers, json=call.json, data=call.form, timeout=30)
    except UNREACHABLE as error:
        # The socket's own words; the request the error carries holds the token.
        raise Refused(f"{call.method} {call.url} failed: {error}") from None
    try:
        return json.loads(answer.content)
    except ValueError:
        return None


# ----------------------------------------------------------------- the mint


@dataclass(frozen=True, slots=True)
class Session:
    """One run's surroundings: its flags, environment, API and output."""

    usage: Usage
    environ: Mapping[str, str]
    api: str
    send: Send
    out: IO[str]
    err: IO[str]

    def say(self, *lines: str) -> None:
        for line in lines:
            print(line, file=self.err)


@dataclass(frozen=True, slots=True)
class Plan:
    body: Mapping[str, Any]
    summary: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Minted:
    id: str
    secret: str = field(repr=False)
    expires: str


@dataclass(frozen=True, slots=True)
class Provider:
    """What differs between two providers' mints; the flow around it is one."""

    api_variable: str
    api_default: str
    # The flags naming a store reference: the secret's first, then the id's.
    stores: tuple[str, ...]
    noun: str
    next_step: str
    unstored: str
    # The request to make, or None when the run has already answered.
    plan: Callable[[Session], Plan | None]
    create: Callable[[Session, Plan], Minted]
    # What goes into each store, in the order of ``stores``.
    values: Callable[[Minted], tuple[str, ...]]


def stores_of(provider: Provider, usage: Usage) -> tuple[Store, ...]:
    """The stores asked for, all on one item; none when the run only plans."""

    given = [usage.text(flag) for flag in provider.stores]
    if not any(given):
        return ()
    flags = " and ".join(f"--{flag.replace('_', '-')}" for flag in provider.stores)
    if not all(given):
        raise UsageError(f"{flags} go together: each names the field its value goes to.")
    stores = tuple(Store.parse(reference) for reference in given)
    if len({(store.vault, store.item) for store in stores}) != 1:
        raise UsageError(f"{flags} name one item.")
    return stores


def mint(provider: Provider, session: Session, onepassword: OnePassword) -> None:
    stores = stores_of(provider, session.usage)
    plan = provider.plan(session)
    if plan is None:
        return
    # A provider shows a secret once, so nothing is created that cannot be stored:
    # a run without stores says what it would create.
    if not stores:
        session.say(*plan.summary, provider.next_step)
        return
    onepassword.check(stores)
    minted = provider.create(session, plan)
    session.say(f"{provider.noun} id: {minted.id}", f"Expires: {minted.expires}")
    try:
        onepassword.write(stores, provider.values(minted))
    except Refused as refused:
        raise Refused(f"{refused} {provider.unstored.format(id=minted.id)}") from None
    session.say(STORED.format(item=stores[0].item, vault=stores[0].vault))


def listed(usage: Usage, flag: str, provider: str, what: str) -> tuple[str, ...]:
    """The entries of the list file ``flag`` names, one per line, comments and
    blank lines skipped; else what ``provider``'s observer credential needs."""

    if not usage.text(flag):
        return tuple(name.api for name in observer_permissions(provider))
    path = Path(usage.text(flag))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise Refused(f"{what} not found: {path}") from None
    return tuple(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))


def required(session: Session, *names: str) -> tuple[str, ...] | None:
    values = tuple(session.environ.get(name, "") for name in names)
    return values if all(values) else None


# ------------------------------------------------------------------ Cloudflare

_PERMISSION = re.compile(r"^(?P<name>.+) \((?P<level>account|zone)\)$")
_SCOPES = {"account": "com.cloudflare.api.account", "zone": "com.cloudflare.api.account.zone"}
_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _cloudflare_result(answer: Any, what: str) -> Any:
    """The result of a call that succeeded, or Cloudflare's own reasons."""

    if isinstance(answer, dict) and answer.get("success") is True:
        return answer.get("result")
    errors = answer.get("errors") if isinstance(answer, dict) else None
    reasons = "; ".join(str(error.get("message")) for error in errors or () if isinstance(error, dict))
    raise Refused(f"Cloudflare refused {what}: {reasons or 'unreadable response'}")


def _cloudflare_token(session: Session) -> str:
    found = required(session, "CLOUDFLARE_BOOTSTRAP_TOKEN")
    if not found:
        raise Refused("Set CLOUDFLARE_BOOTSTRAP_TOKEN to a token that may create API tokens.")
    return found[0]


def _expiry(usage: Usage) -> str:
    expires = usage.text("expires")
    if not expires:
        days = usage.text("days", "90")
        if not days.isascii() or not days.isdigit():
            raise UsageError("--days takes a whole number.")
        expires = (datetime.now(UTC) + timedelta(days=int(days))).strftime("%Y-%m-%d")
    if not _DAY.match(expires):
        raise UsageError("--expires takes YYYY-MM-DD.")
    return expires


def resolve_permissions(lines: Sequence[str], groups: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Each "<name> (<account|zone>)" line as the one group id it names, by level."""

    asked = [(line, _PERMISSION.match(line)) for line in lines]
    malformed = [line for line, match in asked if not match]
    if malformed:
        raise Refused(f"Malformed permission lines: {', '.join(malformed)}")
    resolved: dict[str, list[str]] = {"account": [], "zone": []}
    missing = []
    for line, match in ((line, match) for line, match in asked if match):
        level = match["level"]
        ids = [
            group.get("id")
            for group in groups
            if group.get("name") == match["name"] and _SCOPES[level] in (group.get("scopes") or ())
        ]
        if len(ids) == 1:
            resolved[level].append(str(ids[0]))
        else:
            missing.append(line)
    if missing:
        raise Refused(
            f"Permission groups not found (or not unique): {', '.join(missing)}. "
            "Run with --list-groups to see the current names."
        )
    return resolved


def cloudflare_body(
    account: str, name: str, expires: str, groups: Mapping[str, list[str]], ips: Sequence[str]
) -> dict[str, Any]:
    key = f"com.cloudflare.api.account.{account}"
    resources = {"account": {key: "*"}, "zone": {key: {"com.cloudflare.api.account.zone.*": "*"}}}
    body: dict[str, Any] = {
        "name": name,
        "expires_on": f"{expires}T00:00:00Z",
        "policies": [
            {"effect": "allow", "resources": resources[level], "permission_groups": [{"id": id_} for id_ in ids]}
            for level, ids in groups.items()
            if ids
        ],
    }
    if ips:
        body["condition"] = {"request.ip": {"in": list(ips)}}
    return body


def cloudflare_plan(session: Session) -> Plan | None:
    usage = session.usage
    token = _cloudflare_token(session)
    listing = session.send(Call("GET", f"{session.api}/user/tokens/permission_groups", token=token))
    groups = [
        group for group in _cloudflare_result(listing, "the permission group listing") or () if isinstance(group, dict)
    ]
    if usage.switch("list_groups"):
        for line in sorted(f"{group.get('name')}\t{','.join(group.get('scopes') or ())}" for group in groups):
            print(line, file=session.out)
        return None
    account = usage.text("account")
    if not account:
        raise UsageError("--account is required.")
    if not (account.isascii() and account.isalnum()):
        raise UsageError("The account id is alphanumeric.")
    expires = _expiry(usage)
    lines = listed(usage, "permissions", "cloudflare_api", "Permission list")
    ips = usage.repeated("allow_ip")
    name = usage.text("name", "hq-observer")
    resolved = resolve_permissions(lines, groups)
    count = sum(len(ids) for ids in resolved.values())
    source = f", from {', '.join(ips)}" if ips else ""
    return Plan(
        cloudflare_body(account, name, expires, resolved, ips),
        (f"Would create {name}, expiring {expires}T00:00:00Z, with {count} permission groups{source}.",),
    )


def cloudflare_create(session: Session, plan: Plan) -> Minted:
    created = session.send(Call("POST", f"{session.api}/user/tokens", token=_cloudflare_token(session), json=plan.body))
    result = _cloudflare_result(created, "the new token")
    result = result if isinstance(result, dict) else {}
    if not result.get("id"):
        raise Refused("Cloudflare returned no token id.")
    secret = result.get("value")
    if not isinstance(secret, str) or not secret:
        raise Refused(f"Cloudflare returned no secret. {CLOUDFLARE.unstored.format(id=result['id'])}")
    return Minted(str(result["id"]), secret, str(result.get("expires_on") or "never"))


CLOUDFLARE = Provider(
    api_variable="CLOUDFLARE_API_BASE",
    api_default="https://api.cloudflare.com/client/v4",
    stores=("store",),
    noun="Token",
    next_step=(
        "Nothing created. Rerun with --store naming the cloudflare_api\nconnection's API_TOKEN field (docs/Secrets.md)."
    ),
    unstored="Token {id} was created but not stored. Delete it under API Tokens and run this again.",
    plan=cloudflare_plan,
    create=cloudflare_create,
    values=lambda minted: (minted.secret,),
)


# ------------------------------------------------------------------- Tailscale

_TAILNET = re.compile(r"^[A-Za-z0-9._@-]+$")
# Tailscale's own limit: 50 characters, alphanumerics, hyphens and spaces.
_DESCRIPTION = re.compile(r"^[A-Za-z0-9 -]{1,50}$")


def tailscale_plan(session: Session) -> Plan:
    usage = session.usage
    scopes = listed(usage, "scopes", "tailscale", "Scope list")
    if not _TAILNET.match(usage.text("tailnet", "-")):
        raise UsageError("The tailnet name is malformed.")
    description = usage.text("description", "hq-observer")
    if not _DESCRIPTION.match(description):
        raise UsageError("The description is at most 50 letters, digits, hyphens and spaces.")
    written = [scope for scope in scopes if not scope.endswith(":read")]
    if written:
        raise Refused(f"Every scope must be a read scope: {', '.join(written)}")
    if not scopes:
        raise Refused("The scope list is empty.")
    body: dict[str, Any] = {"keyType": "client", "description": description, "scopes": list(scopes)}
    if tags := usage.repeated("tag"):
        body["tags"] = list(tags)
    return Plan(body, (f"Would create OAuth client {description} with scopes:", *(f"  {scope}" for scope in scopes)))


def _tailscale_token(session: Session) -> str:
    if token := session.environ.get("TAILSCALE_BOOTSTRAP_TOKEN"):
        return token
    client = required(session, "TAILSCALE_BOOTSTRAP_CLIENT_ID", "TAILSCALE_BOOTSTRAP_CLIENT_SECRET")
    if not client:
        raise Refused(
            "Set TAILSCALE_BOOTSTRAP_TOKEN, or TAILSCALE_BOOTSTRAP_CLIENT_ID and TAILSCALE_BOOTSTRAP_CLIENT_SECRET."
        )
    form = {"client_id": client[0], "client_secret": client[1], "scope": "oauth_keys"}
    exchanged = session.send(Call("POST", f"{session.api}/oauth/token", form=form))
    token = exchanged.get("access_token") if isinstance(exchanged, dict) else None
    if not isinstance(token, str) or not token:
        raise Refused("Tailscale did not exchange the bootstrap client for a token.")
    return token


def tailscale_create(session: Session, plan: Plan) -> Minted:
    tailnet = session.usage.text("tailnet", "-")
    token = _tailscale_token(session)
    created = session.send(Call("POST", f"{session.api}/tailnet/{tailnet}/keys", token=token, json=plan.body))
    created = created if isinstance(created, dict) else {}
    if not created.get("id"):
        raise Refused(f"Tailscale refused the new client: {created.get('message') or 'unreadable response'}")
    secret = created.get("key")
    if not isinstance(secret, str) or not secret:
        raise Refused(f"Tailscale returned no secret. {TAILSCALE.unstored.format(id=created['id'])}")
    return Minted(str(created["id"]), secret, "never; OAuth clients stay valid until revoked.")


TAILSCALE = Provider(
    api_variable="TAILSCALE_API_BASE",
    api_default="https://api.tailscale.com/api/v2",
    stores=("store", "store_id"),
    noun="Client",
    next_step=(
        "Nothing created. Rerun with --store and --store-id naming the tailscale\n"
        "connection's CLIENT_SECRET and CLIENT_ID fields (docs/Secrets.md), or\n"
        "create the client under Trust credentials in the admin console with these scopes."
    ),
    unstored="Client {id} was created but not stored. Revoke it under Trust credentials and run this again.",
    plan=tailscale_plan,
    create=tailscale_create,
    values=lambda minted: (minted.secret, minted.id),
)

PROVIDERS: Mapping[str, Provider] = {"cloudflare": CLOUDFLARE, "tailscale": TAILSCALE}


# ------------------------------------------------------------- the GitHub App

ENVIRONMENT = "admission"
# The environment Deploy's Approve job waits on, whose one rule is the deploy
# review App (.github/workflows/deploy.yml, which a pipeline test holds to it).
RELEASE = "release"


def private_key(programs: Programs, vault: str, item: str) -> str:
    """The key from a Document item, or an SSH Key item's private key."""

    found = programs.run(["op", "document", "get", item, "--vault", vault])
    key = found.stdout if found.returncode == 0 else programs.output(["op", "read", f"op://{vault}/{item}/private key"])
    if "PRIVATE KEY-----" not in key:
        raise Refused(f"{item} in {vault} holds no private key.")
    return key


def main_only_environment(programs: Programs, repository: str, name: str, *fields: str) -> str:
    """An environment deployable from main alone, so no branch reads its secret or reaches its job; its API path."""

    path = f"repos/{repository}/environments/{name}"
    programs.output(
        [
            "gh", "api", "-X", "PUT", path,
            "-F", "deployment_branch_policy[protected_branches]=false",
            "-F", "deployment_branch_policy[custom_branch_policies]=true",
            *(word for each in fields for word in ("-F", each)),
        ]
    )  # fmt: skip
    policies = json.loads(programs.output(["gh", "api", f"{path}/deployment-branch-policies"]))
    if not any(policy.get("name") == "main" for policy in policies.get("branch_policies") or ()):
        programs.output(
            ["gh", "api", "-X", "POST", f"{path}/deployment-branch-policies", "-f", "name=main", "-f", "type=branch"]
        )
    return path


def release_environment(programs: Programs, repository: str, app_id: int) -> None:
    """``release``, main only, its one protection rule the deploy review App, which administrators may bypass
    while HQ is down; Deploy refuses to run until it is so."""

    path = main_only_environment(programs, repository, RELEASE, "can_admins_bypass=true")
    rules = json.loads(programs.output(["gh", "api", f"{path}/deployment_protection_rules"]))
    if not any(
        rule.get("app", {}).get("id") == app_id for rule in rules.get("custom_deployment_protection_rules") or ()
    ):
        programs.output(
            ["gh", "api", "-X", "POST", f"{path}/deployment_protection_rules", "-F", f"integration_id={app_id}"]
        )


def wire_github_app(usage: Usage, programs: Programs, out: IO[str], send: Send = https) -> None:
    """The host's HQ_APP_KEY and HQ_APP_CLIENT_ID secrets; on each extension, the key in ``admission`` and the ID.

    With ``--review``: the deploy review App's webhook, its ID in HQ_REVIEW_APP_ID
    for Deploy's check, and the ``release`` environment with it as the rule.

    The client ID is a secret too. A variable is state outside the repository
    that changes what a build does, which the posture standard rejects, and a
    secret stays out of public logs.
    """

    vault, item, client_id = usage.text("vault"), usage.text("item"), usage.text("client_id")
    if not (vault and item and client_id):
        raise UsageError("--vault, --item and --client-id are required.")
    if usage.switch("review"):
        # The review App's key stays with the controller: no secret, here or
        # on an extension, ever holds it.
        if usage.repeated("extension") or not usage.text("webhook_secret"):
            raise UsageError(
                "--review wires the host alone and sets the App's webhook: --webhook-secret, no --extension."
            )
        key = private_key(programs, vault, item)
        app_webhook(usage, programs, key, client_id, out, send, REVIEW_EVENTS)
        app = send(Call("GET", f"{GITHUB_API}/app", token=app_jwt(key, client_id)))
        if not isinstance(app, dict) or not isinstance(app.get("id"), int):
            raise Refused("GitHub did not say which App the key signs for.")
        host = programs.output(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
        programs.output(["gh", "secret", "set", REVIEW_APP_ID, "--repo", host], stdin=str(app["id"]))
        release_environment(programs, host, app["id"])
        print(f"{host}: {REVIEW_APP_ID} set; {RELEASE} environment (main only) waits on {app.get('slug')}.", file=out)
        return
    key = private_key(programs, vault, item)
    host = programs.output(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
    programs.output(["gh", "secret", "set", APP_KEY, "--repo", host], stdin=key)
    programs.output(["gh", "secret", "set", APP_CLIENT_ID, "--repo", host], stdin=client_id)
    print(f"{host}: {APP_KEY} and {APP_CLIENT_ID} set.", file=out)
    for repository in usage.repeated("extension"):
        main_only_environment(programs, repository, ENVIRONMENT)
        programs.output(["gh", "secret", "set", APP_KEY, "--repo", repository, "--env", ENVIRONMENT], stdin=key)
        programs.output(["gh", "secret", "set", APP_CLIENT_ID, "--repo", repository], stdin=client_id)
        print(f"{repository}: {ENVIRONMENT} environment (main only) with {APP_KEY}; {APP_CLIENT_ID} set.", file=out)
    if usage.text("webhook_secret"):
        app_webhook(usage, programs, key, client_id, out, send)


# The routes the inbound-events Worker serves, one per provider: its path and the events it forwards.
ROUTES = Path(__file__).resolve().parents[1] / "worker" / "src" / "generated" / "routes.json"
GITHUB_API = "https://api.github.com"


def app_webhook(
    usage: Usage, programs: Programs, key: str, client_id: str, out: IO[str], send: Send, events: Sequence[str] = ()
) -> None:
    """The App's webhook: the Worker's github route on the inbound host, and the secret the Worker verifies with.

    GitHub sets the address, format and secret only as the App itself, through
    a JWT the App's key signs; whether it is active and the events it sends
    are set on its settings page alone.
    """

    host = usage.text("webhook_host")
    if not host:
        raise UsageError("--webhook-secret needs the inbound host: --webhook-host or HQ_INBOUND_HOSTNAME.")
    (route,) = [route for route in json.loads(ROUTES.read_text())["providers"] if route["provider"] == "github_app"]
    url = f"https://{host}{route['path']}"
    if urlsplit(https_url(url)).netloc != host:
        raise UsageError(f"{host} is not a hostname.")
    secret = programs.output(["op", "read", usage.text("webhook_secret")]).strip()
    if not secret:
        raise Refused("The webhook secret's field is empty.")
    body = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    answer = send(Call("PATCH", f"{GITHUB_API}/app/hook/config", token=app_jwt(key, client_id), json=body))
    if not isinstance(answer, dict) or answer.get("url") != url:
        message = answer.get("message") if isinstance(answer, dict) else None
        raise Refused(f"GitHub refused the App's webhook: {message or 'unreadable response'}")
    print(f"The App's webhook delivers to {url}, signed with the secret the Worker holds.", file=out)
    print(f"On the App's settings page: tick Active and subscribe to {', '.join(events or route['events'])}.", file=out)


def app_jwt(key: str, client_id: str) -> str:
    """A JWT the App's key signs, which GitHub takes as the App itself."""

    now = int(datetime.now(UTC).timestamp())
    # GitHub's limits: issued up to 60 s back for clock drift, expiring within 10 minutes.
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": client_id}, key, algorithm="RS256")


# ------------------------------------------------------------------ the entry


def main(argv: Sequence[str], environ: Mapping[str, str] | None = None) -> int:
    """``mint <provider>`` or ``wire-github-app``, with the flags in the environment."""

    environ = os.environ if environ is None else environ
    usage = Usage.from_environment(environ)
    try:
        match list(argv):
            case ["mint", name] if name in PROVIDERS:
                provider = PROVIDERS[name]
                api = environ.get(provider.api_variable) or provider.api_default
                session = Session(usage, environ, api.rstrip("/"), https, sys.stdout, sys.stderr)
                mint(provider, session, OnePassword())
            case ["wire-github-app"]:
                wire_github_app(usage, Programs(), sys.stdout)
            case _:
                raise UsageError(f"usage: mint {{{','.join(PROVIDERS)}}} | wire-github-app (run through mise)")
    except Refused as refused:
        print(refused, file=sys.stderr)
        return refused.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
