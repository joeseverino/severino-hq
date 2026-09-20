"""Exercise secret delivery failures without real credentials or host writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent

# These scripts run on the host, not in the application container: the image
# carries them so a deploy can sync them out to /usr/local/lib, and they are
# executed there by systemd units. The runtime image therefore does not ship
# jq or curl, and should not -- a hardened container has no use for curl, which
# is the first thing an intruder reaches for.
#
# So the tests run wherever those tools exist, which is every environment these
# scripts actually run in, and skip in the composed-image job rather than
# forcing the image to grow tools for a test's benefit.
REQUIRED_TOOLS = ("jq", "curl")
MISSING_TOOLS = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


@unittest.skipIf(
    MISSING_TOOLS,
    f"host tooling not present here: {', '.join(MISSING_TOOLS)}",
)
class SecretScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "OP_CONNECT_HOST": "",
            "OP_CONNECT_TOKEN": "",
            "FIXTURES": str(self.root),
        }
        self.item = {"fields": [
            {"label": "connection_ref", "value": "example"},
            {"label": "projection", "value": "service_account"},
            {"label": "env_prefix", "value": "EXAMPLE"},
            {"id": "credential", "value": "example-token"},
        ]}
        self.write_item()
        self.stub("op", '''
case "$1 $2" in
    "item list")
        [ "${FAIL_LIST:-0}" = 0 ] || exit 1
        printf '[{"id":"example-item"}]' ;;
    "item get")
        if [ "$3" = "severino-hq env" ]; then
            cat "$FIXTURES/app.json"
        else
            cat "$FIXTURES/item.json"
        fi ;;
    "read "*) printf 'example-token-with-at-least-32-characters' ;;
    *) exit 1 ;;
esac
''')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o700)

    def write_item(self):
        (self.root / "item.json").write_text(json.dumps(self.item))

    def run_script(self, name, *args):
        return subprocess.run(
            ["sh", str(ROOT / "scripts" / name), *args],
            env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_controller_secrets_are_literal_when_sourced(self):
        value = """$(touch "$FIXTURES"/executed) `id` $HOME ' \\"quoted" end"""
        self.item["fields"][-1]["value"] = value
        self.write_item()
        rendered = self.run_script("render-controller-env.sh", "example-vault")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        path = self.root / "rendered"
        path.write_text(rendered.stdout)
        sourced = subprocess.run(
            ["sh", "-c", '. "$1"; printf %s "$EXAMPLE_API_TOKEN"', "sh", str(path)],
            env=self.env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(sourced.stdout, value)
        self.assertFalse((self.root / "executed").exists())

    def test_listing_failure_is_not_an_empty_success(self):
        self.env["FAIL_LIST"] = "1"
        result = self.run_script("render-controller-env.sh", "example-vault")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_prefix_and_multiline_values_are_rejected(self):
        for prefix, value in [("EXAMPLE;id", "token"), ("EXAMPLE", "token\nINJECTED=x")]:
            with self.subTest(prefix=prefix, value=value):
                self.item["fields"][2]["value"] = prefix
                self.item["fields"][-1]["value"] = value
                self.write_item()
                result = self.run_script("render-controller-env.sh", "example-vault")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_failed_refresh_preserves_every_installed_file(self):
        secrets = self.root / "secrets"
        credentials = self.root / "credentials"
        secrets.mkdir()
        credentials.mkdir()
        (credentials / "op_service_account_token").write_text("example-reader-token")
        targets = [secrets / name for name in (
            "severino_mcp_token", "severino_hq_env", "severino_controller_env",
        )]
        for target in targets:
            target.write_text("previous-value")
        (self.root / "app.json").write_text(json.dumps({"fields": [
            {"label": f"EXAMPLE_{index}", "value": "value"} for index in range(15)
        ]}))
        self.stub("install", "exit 0\n")
        self.stub("flock", "exit 0\n")
        self.env.update({
            "CREDENTIALS_DIRECTORY": str(credentials),
            "SEVERINO_HQ_SECRET_DIR": str(secrets),
            "SEVERINO_SECRETS_BACKEND": "service-account",
            "FAIL_LIST": "1",
        })
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        for target in targets:
            self.assertEqual(target.read_text(), "previous-value")
        self.assertEqual([p for p in secrets.glob(".refresh.*") if p.is_dir()], [])

    def test_duplicate_metadata_is_rejected(self):
        self.item["fields"].append({"label": "env_prefix", "value": "OTHER"})
        self.write_item()
        result = self.run_script("render-controller-env.sh", "example-vault")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def connect(self):
        self.env.update({
            "OP_CONNECT_HOST": "http://127.0.0.1:8080",
            "OP_CONNECT_TOKEN": "example.connect.token",
        })
        self.stub("curl", '''
printf '%s\n' "$@" >"$FIXTURES/curl-args"
cat >"$FIXTURES/header"
[ "${FAIL_HTTP:-0}" = 0 ] || exit 22
for arg do url="$arg"; done
case "$url" in
    */vaults) printf '[{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaa","name":"example-vault"}]' ;;
    */items) printf '[{"id":"bbbbbbbbbbbbbbbbbbbbbbbbbb"}]' ;;
    *) exit 1 ;;
esac
''')

    def test_connect_listing_uses_stdin_for_token(self):
        self.connect()
        result = self.run_script("list-secret-items.sh", "example-vault")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{"id": "b" * 26}])
        arguments = (self.root / "curl-args").read_text()
        self.assertNotIn(self.env["OP_CONNECT_TOKEN"], arguments)
        self.assertIn("--noproxy\n*", arguments)
        self.assertNotIn("--location", arguments)
        self.assertEqual((self.root / "header").read_text(),
                         "Authorization: Bearer example.connect.token\n")

    def test_connect_denial_and_remote_endpoint_fail_closed(self):
        self.connect()
        self.env["FAIL_HTTP"] = "1"
        denied = self.run_script("list-secret-items.sh", "example-vault")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(denied.stdout, "")
        (self.root / "curl-args").unlink()
        self.env["OP_CONNECT_HOST"] = "https://example.test"
        remote = self.run_script("list-secret-items.sh", "example-vault")
        self.assertNotEqual(remote.returncode, 0)
        self.assertFalse((self.root / "curl-args").exists())
