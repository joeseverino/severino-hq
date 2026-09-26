"""Exercise secret delivery failures without real credentials or host writes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent

# Host-script tests require host tooling, absent from the runtime image.
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
        self.runtime = self.root / "private-runtime"
        self.runtime.mkdir(mode=0o700)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "OP_CONNECT_HOST": "",
            "OP_CONNECT_TOKEN": "",
            "FIXTURES": str(self.root),
            "SEVERINO_CONTROLLER_SECRET_DIR": str(self.runtime),
            "SEVERINO_SECRETS_VAULT": "Example Vault",
            "SEVERINO_ENV_ITEM": "example env",
        }
        self.env.pop("SEVERINO_CONTROLLER_ENV", None)
        # Simulate Linux ownership and mount metadata; use real file modes.
        self.stub("stat", f'''exec '{sys.executable}' -c '
import os, sys
print("0:" + oct(os.stat(sys.argv[-1]).st_mode & 0o777)[2:])
' "$@"
''')
        self.stub("findmnt", '''
case "$*" in
    *OPTIONS*) printf '/ rw\\n%s %s\\n' "$SEVERINO_CONTROLLER_SECRET_DIR" "${TEST_OPTIONS:-rw,noswap}" ;;
    *) printf "%s\\n" "${TEST_FILESYSTEM:-tmpfs}" ;;
esac
''')
        self.stub("flock", "exit 0\n")
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
        if [ "$3" = "example env" ]; then
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
            stdin=subprocess.DEVNULL,
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

    def prepare_refresh(self):
        secrets = self.root / "secrets"
        credentials = self.root / "credentials"
        secrets.mkdir()
        credentials.mkdir()
        (credentials / "op_service_account_token").write_text("example-reader-token")
        targets = [secrets / "severino_hq_env", self.runtime / "severino_controller_env"]
        for target in targets:
            target.write_text("previous-value")
            target.chmod(0o400)
        # A refresh removes any MCP token file: nothing accepts one.
        self.mcp_token_file = secrets / "severino_mcp_token"
        self.mcp_token_file.write_text("previous-value")
        self.mcp_token_file.chmod(0o400)
        (self.root / "app.json").write_text(json.dumps({"fields": [
            {"label": f"EXAMPLE_{index}", "value": "value"} for index in range(15)
        ]}))
        self.stub("install", "exit 0\n")
        self.stub("flock", "exit 0\n")
        self.stub("chown", "exit 0\n")
        self.stub("docker", "exit 1\n")
        self.env.update({
            "CREDENTIALS_DIRECTORY": str(credentials),
            "SEVERINO_HQ_SECRET_DIR": str(secrets),
            "SEVERINO_SECRETS_BACKEND": "service-account",
        })
        return targets

    def test_failed_refresh_preserves_every_installed_file(self):
        targets = self.prepare_refresh()
        self.env["FAIL_LIST"] = "1"
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        for target in targets:
            self.assertEqual(target.read_text(), "previous-value")
        self.assertEqual([p for p in targets[0].parent.glob(".refresh.*") if p.is_dir()], [])
        self.assertEqual(list(self.runtime.glob(".refresh.*")), [])

    def test_backend_must_be_explicit_before_credentials_are_read(self):
        self.stub("cat", 'touch "$FIXTURES/credential-read"; exit 1\n')
        self.stub("op", 'touch "$FIXTURES/op-called"; exit 1\n')
        for backend in (None, "", "unknown"):
            with self.subTest(backend=backend):
                self.env.pop("SEVERINO_SECRETS_BACKEND", None)
                if backend is not None:
                    self.env["SEVERINO_SECRETS_BACKEND"] = backend
                result = subprocess.run(
                    ["sh", "-eu", "-c", '. "$1"; secrets_backend_select example "$2"',
                     "sh", str(ROOT / "scripts/lib/secrets.sh"), str(ROOT / "scripts")],
                    env=self.env, capture_output=True, text=True, timeout=15,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("backend", result.stderr)
                self.assertFalse((self.root / "credential-read").exists())
                self.assertFalse((self.root / "op-called").exists())

    def test_refresh_stages_on_private_runtime_and_replaces_only_controller_inode(self):
        targets = self.prepare_refresh()
        # Host root can update read-only app files; this test runs unprivileged.
        targets[0].chmod(0o600)
        inodes = [target.stat().st_ino for target in targets]
        real_mktemp = shutil.which("mktemp")
        self.stub("mktemp", f'''printf '%s\\n' "$@" >"$FIXTURES/staging-args"
exec '{real_mktemp}' "$@"
''')
        result = self.run_script("refresh-secrets.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.runtime / ".refresh.XXXXXX"),
                      (self.root / "staging-args").read_text())
        self.assertEqual(targets[0].stat().st_ino, inodes[0])
        self.assertNotEqual(targets[1].stat().st_ino, inodes[1])
        self.assertEqual(targets[1].stat().st_mode & 0o777, 0o400)
        self.assertFalse(self.mcp_token_file.exists())
        current = [target.stat().st_ino for target in targets]
        repeated = self.run_script("refresh-secrets.sh")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("secrets are current", repeated.stdout)
        self.assertEqual([target.stat().st_ino for target in targets], current)
        self.assertEqual(list(self.runtime.glob(".refresh.*")), [])

    def prepare_identity(self, *, public_from=None):
        """An SSH connection whose identity item holds a real key pair."""

        targets = self.prepare_refresh()
        # Host root can update read-only app files; this test runs unprivileged.
        targets[0].chmod(0o600)
        keys = self.root / "keys"
        keys.mkdir()
        for name in ("identity", "other"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                            str(keys / name)], check=True)
        self.item = {"fields": [
            {"label": "connection_ref", "value": "edge"},
            {"label": "projection", "value": "ssh_transport"},
            {"label": "env_prefix", "value": "EDGE"},
            {"label": "host", "value": "edge.example.test"},
            {"label": "port", "value": "2222"},
            {"label": "user", "value": "deploy"},
            {"label": "host_key", "value": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample"},
            {"label": "identity", "value": "Edge deploy key"},
        ]}
        self.write_item()
        public = keys / f"{public_from or 'identity'}.pub"
        self.stub("op", f'''
case "$1 $2" in
    "item list") printf '[{{"id":"example-item"}}]' ;;
    "item get")
        if [ "$3" = "example env" ]; then cat "$FIXTURES/app.json"; else cat "$FIXTURES/item.json"; fi ;;
    "read op://Example Vault/Edge deploy key/private key?ssh-format=openssh") cat '{keys / "identity"}' ;;
    "read op://Example Vault/Edge deploy key/public key") cat '{public}' ;;
    *) exit 1 ;;
esac
''')
        return targets, keys

    def test_ssh_identities_render_to_the_secret_mount_with_pinned_hosts(self):
        _, keys = self.prepare_identity()
        result = self.run_script("refresh-secrets.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        live = self.runtime / "ssh"
        self.assertEqual((live / "edge").read_text(), (keys / "identity").read_text())
        self.assertEqual((live / "edge").stat().st_mode & 0o777, 0o400)
        self.assertEqual((live / "known_hosts").read_text(),
                         "[edge.example.test]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample\n")
        self.assertEqual(list(self.runtime.glob(".refresh.*")), [])

    def test_an_identity_whose_halves_differ_is_refused_and_nothing_changes(self):
        self.prepare_identity(public_from="other")
        live = self.runtime / "ssh"
        live.mkdir(mode=0o700)
        (live / "edge").write_text("previous-key")
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not match", result.stderr)
        self.assertEqual((live / "edge").read_text(), "previous-key")

    def test_a_removed_connection_takes_its_identity_with_it(self):
        self.prepare_identity()
        live = self.runtime / "ssh"
        live.mkdir(mode=0o700)
        (live / "retired").write_text("an old key")
        result = self.run_script("refresh-secrets.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((live / "retired").exists())
        self.assertTrue((live / "edge").exists())

    def test_a_tab_in_a_connection_field_is_refused(self):
        self.prepare_identity()
        self.item["fields"][3]["value"] = "edge.example.test\tinjected"
        self.write_item()
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / "ssh").exists())

    def test_an_identity_must_name_an_item_not_a_reference(self):
        self.prepare_identity()
        self.item["fields"][-1]["value"] = "op://Elsewhere/key"
        self.write_item()
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)

    def controller_contract(self, command):
        return subprocess.run(
            ["sh", "-c", '. "$1"; ' + command, "sh",
             str(ROOT / "scripts/lib/controller-env.sh")],
            env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_controller_refuses_shared_directory_and_legacy_override(self):
        for changes in (
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/severino-hq"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/severino-hq/credentials"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run//severino-hq"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/./severino-hq"},
            {"SEVERINO_CONTROLLER_ENV": "/run/severino-hq/severino_controller_env"},
        ):
            with self.subTest(changes=changes):
                original = self.env.copy()
                self.env.update(changes)
                result = self.controller_contract("exit 0")
                self.env = original
                self.assertNotEqual(result.returncode, 0)

    def test_controller_refuses_writable_or_symlinked_credentials(self):
        credential = self.runtime / "severino_controller_env"
        credential.write_text("EXAMPLE='value'\n")
        credential.chmod(0o400)
        self.assertEqual(self.controller_contract("controller_require_environment").returncode, 0)
        credential.chmod(0o600)
        self.assertNotEqual(self.controller_contract("controller_require_environment").returncode, 0)
        credential.chmod(0o400)
        self.runtime.chmod(0o755)
        self.assertNotEqual(self.controller_contract("controller_require_environment").returncode, 0)
        self.runtime.chmod(0o700)
        saved = self.runtime / "saved"
        credential.rename(saved)
        credential.symlink_to(saved)
        self.assertNotEqual(self.controller_contract("controller_require_environment").returncode, 0)

    def test_disk_backed_runtime_is_rejected_before_fetching_secrets(self):
        self.env["TEST_FILESYSTEM"] = "ext4"
        self.env["SEVERINO_HQ_SECRET_DIR"] = str(self.root / "secrets")
        self.stub("install", "exit 0\n")
        self.stub("op", 'touch "$FIXTURES/op-called"; exit 1\n')
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tmpfs", result.stderr)
        self.assertFalse((self.root / "op-called").exists())

    def test_swappable_runtime_is_rejected_before_fetching_secrets(self):
        self.prepare_refresh()
        self.env["TEST_OPTIONS"] = "rw,nosuid,noswapfile"
        self.stub("op", 'touch "$FIXTURES/op-called"; exit 1\n')
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("noswap", result.stderr)
        self.assertFalse((self.root / "op-called").exists())

    def test_private_keys_left_on_disk_stop_the_refresh(self):
        self.prepare_refresh()
        self.stub("op", 'touch "$FIXTURES/op-called"; exit 1\n')
        legacy = self.root / "secrets" / "ssh"
        legacy.mkdir()
        (legacy / "edge.pub").write_text("ssh-ed25519 AAAA example\n")
        (legacy / "edge").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n")
        result = self.run_script("refresh-secrets.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(legacy), result.stderr)
        self.assertFalse((self.root / "op-called").exists())
        self.assertTrue((legacy / "edge").exists())

    def test_public_halves_alone_on_disk_do_not_stop_the_refresh(self):
        self.prepare_identity()
        legacy = self.root / "secrets" / "ssh"
        legacy.mkdir()
        (legacy / "edge.pub").write_text("ssh-ed25519 AAAA example\n")
        result = self.run_script("refresh-secrets.sh")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_resolves_shell_quoted_reference_literally(self):
        credential = self.runtime / "severino_controller_env"
        credential.write_text("EXAMPLE_CONNECTION_REF='example'\n"
                              "EXAMPLE_HOST='example.test'\n"
                              "EXAMPLE_PORT='22'\nEXAMPLE_USER='reader'\n")
        credential.chmod(0o400)
        self.stub("id", "printf '0\\n'\n")
        self.stub("ssh", 'printf "%s\\n" "$@"\n')
        result = self.run_script("controller-ssh.sh", "example", "preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reader@example.test\npreflight\n", result.stdout)
        for reference, operation in [(".*", "preflight"), ("example", "shell")]:
            denied = self.run_script("controller-ssh.sh", reference, operation)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stdout, "")

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
