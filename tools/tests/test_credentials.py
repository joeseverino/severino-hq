"""The credential tasks against a fake API, op and gh: nothing leaves the machine.

What every case holds: a minted secret reaches the 1Password item and nothing
else: no stdout, argument list, URL or message.
"""

import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from hq.platform.application.github_posture import WIRED_SECRETS
from hq.platform.core.tests.task import Usage
from tests.mise_tasks import body, mise_tasks
from tools import credentials
from tools.credentials import Call, OnePassword, Programs, Refused, Session

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = "bootstrap-secret-0123456789"
MINTED = "minted-secret-9876543210"
EXCHANGED = "exchanged-token-555"
CF_API = "https://api.example.test"
TS_API = "https://ts.example.test"

GROUPS = [
    {"id": "g-acct-settings", "name": "Account Settings Read", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-acct-analytics", "name": "Account Analytics Read", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-registrar", "name": "Registrar Domains Read", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-zone-settings", "name": "Zone Settings Read", "scopes": ["com.cloudflare.api.account.zone"]},
    {"id": "g-zone-analytics", "name": "Analytics Read", "scopes": ["com.cloudflare.api.account.zone"]},
]
ANSWERS = {
    ("GET", f"{CF_API}/user/tokens/permission_groups"): {"success": True, "result": GROUPS},
    ("POST", f"{CF_API}/user/tokens"): {
        "success": True,
        "result": {"id": "tok-1", "expires_on": "2030-01-01T00:00:00Z", "value": MINTED},
    },
    ("POST", f"{TS_API}/oauth/token"): {"access_token": EXCHANGED},
    ("POST", f"{TS_API}/tailnet/-/keys"): {"id": "client-1", "key": MINTED, "keyType": "client"},
}
ITEM = {
    "id": "item-1",
    "fields": [
        {"id": "username", "label": "username", "value": "old-id"},
        {"id": "credential", "label": "credential", "value": "old-secret"},
        {"id": "c", "label": "connection_ref", "value": "example-api"},
    ],
}
KNOWN = ("Account Settings Read (account)", "Account Analytics Read (account)")
KNOWN += ("Registrar Domains Read (account)", "Zone Settings Read (zone)")


class Api:
    """Canned answers by method and URL; every call recorded."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def __call__(self, call: Call):
        self.calls.append(call)
        return ANSWERS.get((call.method, call.url), {"success": False, "errors": [{"message": "unexpected request"}]})

    def posted(self) -> list[Call]:
        return [call for call in self.calls if call.method == "POST"]


class Commands:
    """op and gh: each command and its stdin recorded; ``item edit`` keeps the template it is piped."""

    def __init__(self, answers: dict[tuple[str, ...], str] | None = None) -> None:
        self.answers = answers or {}
        self.argv: list[list[str]] = []
        self.stdin: list[str] = []
        self.edited: dict | None = None

    def __call__(self, command, *, input=None, **_kwargs):  # noqa: A002 - subprocess.run's own keyword
        self.argv.append(command)
        self.stdin.append(input or "")
        if command[:3] == ["op", "item", "get"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(ITEM), "")
        if command[:3] == ["op", "item", "edit"]:
            self.edited = json.loads(input)
            return subprocess.CompletedProcess(command, 0, "", "")
        for prefix, answer in self.answers.items():
            if tuple(command[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(command, 0, answer, "")
        return subprocess.CompletedProcess(command, 0, "", "")


def declared_flags(provider: str) -> set[str]:
    """The flags the mint task declares, as the ``usage_*`` names mise sets."""

    usage = body(mise_tasks()[f"mint:{provider}"])
    return {name.replace("-", "_") for name in re.findall(r'#USAGE flag\s+"--([\w-]+)', usage)}


class Run:
    def __init__(self, provider: str, flags: dict[str, str], environ: dict[str, str]) -> None:
        # A case that passes a flag the task does not declare tests nothing mise could hand it.
        undeclared = set(flags) - declared_flags(provider)
        assert not undeclared, f"mint:{provider} declares no {sorted(undeclared)}"
        self.api, self.commands = Api(), Commands()
        out, err = io.StringIO(), io.StringIO()
        spec = credentials.PROVIDERS[provider]
        base = CF_API if provider == "cloudflare" else TS_API
        session = Session(Usage(flags), environ, base, self.api, out, err)
        self.refused: Refused | None = None
        try:
            credentials.mint(spec, session, OnePassword(Programs(self.commands)))
        except Refused as refused:
            self.refused = refused
        self.out, self.err = out.getvalue(), err.getvalue() + (str(self.refused) if self.refused else "")

    def field(self, name: str) -> list[str]:
        assert self.edited is not None
        return [entry["value"] for entry in self.edited["fields"] if entry["id"] == name]

    @property
    def edited(self) -> dict | None:
        return self.commands.edited

    def assert_secret_kept(self, test: SimpleTestCase, *secrets: str) -> None:
        """No secret in an op argument, an API address, stdout or stderr."""

        for secret in secrets:
            test.assertFalse(any(secret in word for argv in self.commands.argv for word in argv))
            test.assertFalse(any(secret in call.url for call in self.api.calls))
            test.assertNotIn(secret, self.out)
            test.assertNotIn(secret, self.err)


def cloudflare(*, token: str = BOOTSTRAP, permissions: tuple[str, ...] = KNOWN, **flags: str) -> Run:
    with tempfile.TemporaryDirectory() as scratch:
        listed = Path(scratch) / "permissions.txt"
        listed.write_text("# a comment\n\n" + "\n".join(permissions) + "\n")
        return Run(
            "cloudflare",
            {"account": "abc123", "permissions": str(listed), **flags},
            {"CLOUDFLARE_BOOTSTRAP_TOKEN": token} if token else {},
        )


def tailscale(environ: dict[str, str] | None = None, **flags: str) -> Run:
    return Run("tailscale", flags, {"TAILSCALE_BOOTSTRAP_TOKEN": BOOTSTRAP} if environ is None else environ)


@tag("source")
class CloudflareMintTests(SimpleTestCase):
    def test_no_bootstrap_token_makes_no_call(self):
        run = cloudflare(token="")

        self.assertIsNotNone(run.refused)
        self.assertIn("CLOUDFLARE_BOOTSTRAP_TOKEN", run.err)
        self.assertEqual(run.api.calls, [])

    def test_without_a_store_nothing_is_created(self):
        run = cloudflare()

        self.assertIsNone(run.refused)
        self.assertEqual(run.out, "")
        self.assertIn("Nothing created", run.err)
        self.assertEqual(run.api.posted(), [])

    def test_a_stored_token_reports_its_id_and_expiry(self):
        run = cloudflare(expires="2030-01-01", store="op://Example Vault/item-1/credential")

        self.assertIn("Token id: tok-1", run.err)
        self.assertIn("Expires: 2030-01-01", run.err)
        self.assertTrue(all(call.token == BOOTSTRAP for call in run.api.calls))

    def test_permission_names_resolve_to_ids_under_the_right_resources(self):
        run = cloudflare(expires="2030-01-01", allow_ip="'192.0.2.0/24'", store="op://Example Vault/item-1/credential")

        (created,) = run.api.posted()
        body = created.json
        assert body is not None
        self.assertEqual(body["expires_on"], "2030-01-01T00:00:00Z")
        self.assertEqual(body["condition"]["request.ip"]["in"], ["192.0.2.0/24"])
        key = "com.cloudflare.api.account.abc123"
        granted = {
            "account": sorted(
                g["id"] for p in body["policies"] if p["resources"][key] == "*" for g in p["permission_groups"]
            ),
            "zone": [
                g["id"]
                for p in body["policies"]
                if isinstance(p["resources"][key], dict)
                for g in p["permission_groups"]
            ],
        }
        self.assertEqual(granted["account"], ["g-acct-analytics", "g-acct-settings", "g-registrar"])
        self.assertEqual(granted["zone"], ["g-zone-settings"])

    def test_an_unknown_permission_group_stops_the_mint_and_is_named(self):
        run = cloudflare(
            permissions=("Account Settings Read (account)", "Workers Scripts Read (account)"),
            store="op://Example Vault/item-1/credential",
        )

        self.assertIsNotNone(run.refused)
        self.assertIn("Workers Scripts Read (account)", run.err)
        self.assertEqual(run.api.posted(), [])

    def test_store_writes_the_secret_into_the_items_field_only(self):
        run = cloudflare(store="op://Example Vault/item-1/credential")

        self.assertIsNone(run.refused)
        self.assertEqual(run.out, "")
        self.assertEqual(run.field("credential"), [MINTED])
        self.assertEqual(run.field("username"), ["old-id"])
        run.assert_secret_kept(self, BOOTSTRAP, MINTED)

    def test_a_store_field_the_item_lacks_stops_before_anything_is_created(self):
        run = cloudflare(store="op://Example Vault/item-1/missing")

        self.assertIsNotNone(run.refused)
        self.assertEqual(run.api.posted(), [])
        self.assertIsNone(run.edited)

    def test_a_malformed_store_reference_is_refused_before_any_call(self):
        for reference in ("op://Example Vault/item-1/section/credential", "op://Example Vault/item-1", "item-1"):
            with self.subTest(reference=reference):
                run = cloudflare(store=reference)

                self.assertIsNotNone(run.refused)
                self.assertEqual((run.api.calls, run.commands.argv), ([], []))


@tag("source")
class TailscaleMintTests(SimpleTestCase):
    def test_without_a_store_it_prints_the_scopes_and_calls_nothing(self):
        run = tailscale()

        self.assertIsNone(run.refused)
        self.assertEqual(run.out, "")
        self.assertIn("devices:core:read", run.err)
        self.assertIn("Nothing created", run.err)
        self.assertEqual(run.api.calls, [])

    def test_the_client_is_created_with_read_scopes_only(self):
        run = tailscale(
            tag="'tag:example' 'tag:other'",
            store="op://Example Vault/item-1/credential",
            store_id="op://Example Vault/item-1/username",
        )

        (created,) = run.api.posted()
        assert created.json is not None
        self.assertEqual(created.json["keyType"], "client")
        self.assertTrue(created.json["scopes"])
        self.assertTrue(all(scope.endswith(":read") for scope in created.json["scopes"]))
        self.assertEqual(created.json["tags"], ["tag:example", "tag:other"])

    def test_a_bootstrap_client_is_exchanged_for_a_token(self):
        environ = {"TAILSCALE_BOOTSTRAP_CLIENT_ID": "boot-id", "TAILSCALE_BOOTSTRAP_CLIENT_SECRET": BOOTSTRAP}
        run = tailscale(
            environ, store="op://Example Vault/item-1/credential", store_id="op://Example Vault/item-1/username"
        )

        exchange, created = run.api.posted()
        self.assertEqual(run.field("credential"), [MINTED])
        self.assertEqual((exchange.form or {}).get("client_secret"), BOOTSTRAP)
        self.assertEqual(created.token, EXCHANGED)
        run.assert_secret_kept(self, BOOTSTRAP, EXCHANGED, MINTED)

    def test_no_bootstrap_credential_creates_nothing(self):
        run = tailscale(
            environ={}, store="op://Example Vault/item-1/credential", store_id="op://Example Vault/item-1/username"
        )

        self.assertIsNotNone(run.refused)
        self.assertEqual(run.api.calls, [])

    def test_store_writes_the_secret_and_the_client_id_into_the_item(self):
        run = tailscale(store="op://Example Vault/item-1/credential", store_id="op://Example Vault/item-1/username")

        self.assertIsNone(run.refused)
        self.assertEqual(run.out, "")
        self.assertEqual(run.field("credential"), [MINTED])
        self.assertEqual(run.field("username"), ["client-1"])
        run.assert_secret_kept(self, BOOTSTRAP, MINTED)

    def test_the_id_and_secret_go_to_one_item_and_together(self):
        for flags in (
            {"store": "op://Example Vault/item-1/credential", "store_id": "op://Other/item-2/username"},
            {"store": "op://Example Vault/item-1/credential"},
        ):
            with self.subTest(flags=flags):
                run = tailscale(**flags)

                self.assertIsNotNone(run.refused)
                self.assertEqual((run.api.calls, run.commands.argv), ([], []))

    def test_a_write_scope_is_refused_before_any_call(self):
        with tempfile.TemporaryDirectory() as scratch:
            scopes = Path(scratch) / "scopes.txt"
            scopes.write_text("devices:core:read\npolicy_file\n")
            run = tailscale(
                scopes=str(scopes),
                store="op://Example Vault/item-1/credential",
                store_id="op://Example Vault/item-1/username",
            )

        self.assertIsNotNone(run.refused)
        self.assertIn("policy_file", run.err)
        self.assertEqual(run.api.calls, [])


@tag("source")
class WireGithubAppTests(SimpleTestCase):
    KEY = "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----\n"

    def test_the_secrets_it_wires_are_the_ones_posture_checks(self):
        self.assertEqual(WIRED_SECRETS, {credentials.APP_KEY, credentials.APP_CLIENT_ID})

    def wire(self, *extensions: str) -> tuple[Commands, str]:
        commands = Commands(
            {
                ("op", "document", "get"): self.KEY,
                ("gh", "repo", "view"): "example/host\n",
                ("gh", "api", "repos/example/ext/environments/admission/deployment-branch-policies"): "{}",
            }
        )
        out = io.StringIO()
        usage = {"vault": "Example Vault", "item": "example app", "client_id": "Iv1.example"}
        if extensions:
            usage["extension"] = " ".join(extensions)
        credentials.wire_github_app(Usage(usage), Programs(commands), out)
        return commands, out.getvalue()

    def secrets_set(self, commands: Commands) -> dict[tuple[str, ...], str]:
        return {
            tuple(argv[3:]): stdin
            for argv, stdin in zip(commands.argv, commands.stdin, strict=True)
            if argv[:3] == ["gh", "secret", "set"]
        }

    def test_the_host_gets_the_key_and_client_id_on_stdin(self):
        commands, out = self.wire()

        self.assertEqual(
            self.secrets_set(commands),
            {
                ("HQ_APP_KEY", "--repo", "example/host"): self.KEY,
                ("HQ_APP_CLIENT_ID", "--repo", "example/host"): "Iv1.example",
            },
        )
        self.assertFalse(any(self.KEY in word for argv in commands.argv for word in argv))
        self.assertNotIn("PRIVATE KEY", out)

    def test_an_extension_gets_the_key_in_a_main_only_admission_environment(self):
        commands, _ = self.wire("example/ext")

        self.assertEqual(
            self.secrets_set(commands)[("HQ_APP_KEY", "--repo", "example/ext", "--env", "admission")], self.KEY
        )
        self.assertIn(("HQ_APP_CLIENT_ID", "--repo", "example/ext"), self.secrets_set(commands))
        self.assertIn(
            ["gh", "api", "-X", "POST", "repos/example/ext/environments/admission/deployment-branch-policies",
             "-f", "name=main", "-f", "type=branch"],
            commands.argv,
        )  # fmt: skip

    def test_an_item_without_a_private_key_sets_nothing(self):
        commands = Commands({("op", "document", "get"): "not a key", ("op", "read"): "not a key"})
        usage = Usage({"vault": "Example Vault", "item": "example app", "client_id": "Iv1.example"})

        with self.assertRaises(Refused):
            credentials.wire_github_app(usage, Programs(commands), io.StringIO())
        self.assertEqual(self.secrets_set(commands), {})


@tag("source")
class TaskTests(SimpleTestCase):
    def test_the_task_runs_as_printed(self):
        """Through mise, from another directory: the plan, on stderr, with nothing created."""

        environ = {name: value for name, value in os.environ.items() if not name.startswith("TAILSCALE_")}
        done = subprocess.run(
            ["mise", "run", "mint:tailscale", "--tag", "tag:example"],
            cwd=ROOT / "docs",
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertIn("Nothing created", done.stderr)

    def test_an_unknown_flag_is_refused_before_the_task_runs(self):
        done = subprocess.run(
            ["mise", "run", "mint:cloudflare", "--account", "abc123", "--bogus"],
            cwd=ROOT,
            env={**os.environ, "CLOUDFLARE_API_BASE": "http://192.0.2.1"},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--bogus", done.stderr)
