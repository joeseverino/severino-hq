"""How provider connections reach the controller: one document, never the environment.

The renderer that writes the document is Go (``controller/cmd/hq-secrets``) and
is tested there. These hold the shell that carries it the last step: the
checks root makes before reading it, and the launch arguments.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = ROOT / "scripts" / "run-controller.sh"

# Values that must reach the container only inside the mounted document.
SENTINEL = "sentinel-provider-token-value"
REFERENCE = "sentinel-connection"

# Every variable the launcher may set: a path, a name, an image or a nonce.
LAUNCH_VARIABLES = {
    "HQ_CONTROLLER_RUN", "HQ_IN_PROCESS", "HQ_MANAGE_PY", "HQ_CONTROLLER_CONNECTIONS",
    "HQ_CONTROLLER_SSH_DIR", "HQ_ACME_DIR", "HQ_CONTROLLER_IMAGE",
    "SEVERINO_HQ_SOURCE_REPOSITORY", "SEVERINO_REGISTRY_DOORBELL", "HQ_CONTROLLER_CA_FILE",
    "SEVERINO_TAILNET_STATUS", "SEVERINO_TAILNET_LOCK", "SEVERINO_HOST_FIREWALL",
}


class Host(unittest.TestCase):
    """A relocated host: stubs for what needs root, real file modes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runtime = self.root / "private-runtime"
        self.runtime.mkdir(mode=0o700)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FIXTURES": str(self.root),
            "SEVERINO_CONTROLLER_SECRET_DIR": str(self.runtime),
        }
        self.env.pop("SEVERINO_CONTROLLER_ENV", None)
        # Simulate Linux ownership and mount metadata; use real file modes. The
        # web environment on the tmpfs belongs to the web user, all else to root.
        self.stub("stat", f'''exec '{sys.executable}' -c '
import os, sys
path, fmt = sys.argv[-1], sys.argv[-2]
st = os.stat(path)
mode = oct(st.st_mode & 0o777)[2:]
runtime = os.environ["SEVERINO_CONTROLLER_SECRET_DIR"]
web_file = path.startswith(os.path.join(runtime, "web") + os.sep) and os.path.isfile(path)
uid = "0" if path.startswith(runtime) and not web_file else "10001"
if fmt == "%u %h":
    print(uid, st.st_nlink)
elif fmt == "%u %a":
    print(uid, mode)
else:
    print("0:" + mode)
' "$@"
''')
        self.stub("findmnt", '''
case "$*" in
    *OPTIONS*) printf '/ rw\\n%s %s\\n' "$SEVERINO_CONTROLLER_SECRET_DIR" "${TEST_OPTIONS:-rw,noswap}" ;;
    *) printf "%s\\n" "${TEST_FILESYSTEM:-tmpfs}" ;;
esac
''')
        self.stub("flock", "exit 0\n")
        self.document = self.runtime / "controller-connections.json"
        self.write_document()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o700)

    def write_document(self, connections=None):
        if self.document.exists():
            self.document.chmod(0o600)
        self.document.write_text(json.dumps({"schema_version": 1, "connections": connections or [
            {"ref": REFERENCE, "prefix": "EXAMPLE", "values": {
                "CONNECTION_REF": REFERENCE, "API_TOKEN": SENTINEL, "HOST": "example.test",
                "PORT": "22", "USER": "reader"}},
        ]}))
        self.document.chmod(0o400)

    def run_script(self, name, *args):
        return subprocess.run(
            ["sh", str(ROOT / "scripts" / name), *args],
            env=self.env, capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )

    def contract(self, command):
        return subprocess.run(
            ["sh", "-c", '. "$1"; ' + command, "sh", str(ROOT / "scripts/lib/controller-env.sh")],
            env=self.env, capture_output=True, text=True, timeout=15,
        )


class ConnectionsContractTests(Host):
    def test_shared_directory_and_legacy_override_are_refused(self):
        for changes in (
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/severino-hq"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/severino-hq/credentials"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run//severino-hq"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "/run/./severino-hq"},
            {"SEVERINO_CONTROLLER_SECRET_DIR": "run/severino-hq-secrets"},
            {"SEVERINO_CONTROLLER_ENV": "/run/severino-hq/severino_controller_env"},
            {"SEVERINO_CONTROLLER_ENV": str(self.runtime / "severino_controller_env")},
        ):
            with self.subTest(changes=changes):
                original = self.env.copy()
                self.env.update(changes)
                result = self.contract("exit 0")
                self.env = original
                self.assertNotEqual(result.returncode, 0)

    def test_writable_or_symlinked_connections_are_refused(self):
        self.assertEqual(self.contract("controller_require_connections").returncode, 0)
        self.document.chmod(0o600)
        self.assertNotEqual(self.contract("controller_require_connections").returncode, 0)
        self.document.chmod(0o400)
        self.runtime.chmod(0o755)
        self.assertNotEqual(self.contract("controller_require_connections").returncode, 0)
        self.runtime.chmod(0o700)
        saved = self.runtime / "saved"
        self.document.rename(saved)
        self.assertNotEqual(self.contract("controller_require_connections").returncode, 0)
        self.document.symlink_to(saved)
        self.assertNotEqual(self.contract("controller_require_connections").returncode, 0)
        self.document.unlink()
        self.document.write_text("")
        self.document.chmod(0o400)
        self.assertNotEqual(self.contract("controller_require_connections").returncode, 0)

    def test_disk_backed_or_swappable_runtime_is_refused(self):
        for change, message in (({"TEST_FILESYSTEM": "ext4"}, "tmpfs"),
                                ({"TEST_OPTIONS": "rw,nosuid,noswapfile"}, "noswap")):
            with self.subTest(change=change):
                original = self.env.copy()
                self.env.update(change)
                result = self.contract("controller_require_connections")
                self.env = original
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)


@unittest.skipIf(shutil.which("jq") is None, "host tooling not present here: jq")
class ControllerSSHTests(Host):
    def setUp(self):
        super().setUp()
        self.stub("id", "printf '0\\n'\n")
        self.stub("ssh", 'printf "%s\\n" "$@"\n')

    def test_the_connection_is_read_from_the_document_as_data(self):
        result = self.run_script("controller-ssh.sh", REFERENCE, "preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reader@example.test\npreflight\n", result.stdout)
        self.assertIn(f"-i\n{self.runtime}/ssh/{REFERENCE}\n", result.stdout)
        self.assertIn("-p\n22\n", result.stdout)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)

    def test_unknown_references_and_operations_are_refused(self):
        for reference, operation in [(".*", "preflight"), ("", "preflight"), ("other", "preflight"),
                                     ('" or true or "', "preflight"), (REFERENCE, "shell"),
                                     ("../" + REFERENCE, "preflight")]:
            with self.subTest(reference=reference, operation=operation):
                denied = self.run_script("controller-ssh.sh", reference, operation)
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(denied.stdout, "")

    def test_a_destination_that_reads_as_an_option_is_refused(self):
        for values in ({"HOST": "-oProxyCommand=x"}, {"USER": "root@other"}, {"PORT": "22 -oProxyCommand=x"},
                       {"HOST": ""}, {"USER": "-l"}):
            with self.subTest(values=values):
                self.write_document([{"ref": REFERENCE, "prefix": "EXAMPLE", "values": {
                    "CONNECTION_REF": REFERENCE, "HOST": "example.test", "PORT": "22", "USER": "reader", **values}}])
                denied = self.run_script("controller-ssh.sh", REFERENCE, "preflight")
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(denied.stdout, "")

    def test_a_connection_that_is_not_ssh_is_refused(self):
        self.write_document([{"ref": REFERENCE, "prefix": "EXAMPLE",
                              "values": {"CONNECTION_REF": REFERENCE, "API_TOKEN": SENTINEL}}])
        denied = self.run_script("controller-ssh.sh", REFERENCE, "preflight")
        self.assertNotEqual(denied.returncode, 0)
        self.assertNotIn(SENTINEL, denied.stdout + denied.stderr)


class LauncherTests(Host):
    """The launch: what `docker run` is given, with docker itself a recorder."""

    def setUp(self):
        super().setUp()
        self.app = self.root / "app"
        (self.app / "secrets").mkdir(parents=True)
        web = self.runtime / "web"
        web.mkdir(mode=0o700)
        (web / "severino_hq_env").write_text("EXAMPLE='value'\n")
        identities = self.runtime / "ssh"
        identities.mkdir(mode=0o700)
        (identities / REFERENCE).write_text("an identity\n")
        self.env.update({"SEVERINO_HQ_APP_DIR": str(self.app)})
        self.stub("id", "printf '0\\n'\n")
        self.stub("chown", "exit 0\n")
        self.stub("curl", "exit 1\n")
        self.stub("nft", "exit 1\n")
        # install -d makes a directory; otherwise it copies with the mode given.
        self.stub("install", '''
mode=""; directory=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        -d) directory=1; shift ;;
        -m) mode="$2"; shift 2 ;;
        -o|-g) shift 2 ;;
        *) break ;;
    esac
done
if [ "$directory" -eq 1 ]; then
    for last; do :; done
    mkdir -p "$last"; [ -z "$mode" ] || chmod "$mode" "$last"
else
    cp "$1" "$2"; [ -z "$mode" ] || chmod "$mode" "$2"
fi
''')
        # Records the launch and keeps a copy of the mounted document, which
        # is gone once the launcher's trap has run.
        self.stub("docker", '''
case "$1" in
    inspect)
        case "$*" in
            *Config.Image*) echo example-image ;;
            *Mounts*) echo example-volume ;;
            *) echo https://github.com/example/example ;;
        esac ;;
    run)
        printf '%s\\n' "$@" >"$FIXTURES/docker-args"
        env >"$FIXTURES/docker-env"
        for argument; do
            case "$argument" in
                type=bind,source=*,target=/run/secrets/controller-connections.json,readonly)
                    source="${argument#type=bind,source=}"
                    cp "${source%%,target=*}" "$FIXTURES/mounted-connections" ;;
            esac
        done ;;
esac
''')

    def launch(self, *args):
        return self.run_script("run-controller.sh", *args)

    def arguments(self):
        return (self.root / "docker-args").read_text().splitlines()

    def test_connections_are_mounted_and_never_passed_as_environment(self):
        result = self.launch("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.arguments()
        # The document is what the controller reads, byte for byte.
        self.assertEqual((self.root / "mounted-connections").read_text(), self.document.read_text())
        self.assertIn("HQ_CONTROLLER_CONNECTIONS=/run/secrets/controller-connections.json", arguments)
        mounts = [a for a in arguments if "target=/run/secrets/controller-connections.json" in a]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0].endswith(",readonly"))
        self.assertTrue(mounts[0].startswith(f"type=bind,source={self.runtime}/run."))
        # Every --env is NAME=VALUE with a known, non-secret name: no bare
        # `--env NAME`, which would lift a value out of the launcher's own
        # environment into the container's configuration.
        passed = [arguments[i + 1] for i, a in enumerate(arguments) if a in ("--env", "-e")]
        self.assertTrue(passed)
        for entry in passed:
            with self.subTest(entry=entry.split("=")[0]):
                name, separator, _ = entry.partition("=")
                self.assertEqual(separator, "=", "a bare --env NAME forwards the launcher's environment")
                self.assertIn(name, LAUNCH_VARIABLES)
        self.assertFalse([a for a in arguments if a.startswith(("--env=", "-e=", "--env-file"))])
        # Nothing of the document reaches the arguments or docker's environment:
        # not a value, not a reference, not a name derived from a prefix.
        for surface in ("\n".join(arguments), (self.root / "docker-env").read_text()):
            self.assertNotIn(SENTINEL, surface)
            self.assertNotIn(REFERENCE, surface)
            self.assertNotIn("EXAMPLE_", surface)
            self.assertNotIn("API_TOKEN", surface)
        self.assertEqual(arguments[-1], "--apply")
        # The run's staging is gone when the launcher returns.
        self.assertEqual(list(self.runtime.glob("run.*")), [])

    def test_the_application_environment_is_copied_under_the_shared_lock(self):
        # The renderer rewrites it in place, truncating first, under the
        # exclusive lock: a copy taken outside the shared one can be cut short.
        self.stub("flock", 'echo lock >>"$FIXTURES/order"\n')
        install = (self.bin / "install").read_text().replace(
            "set -eu\n", 'set -eu\necho "install $*" >>"$FIXTURES/order"\n', 1)
        (self.bin / "install").write_text(install)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        order = (self.root / "order").read_text().splitlines()
        copies = [i for i, line in enumerate(order)
                  if line.startswith("install ") and line.endswith("/env") and "severino_hq_env" in line]
        self.assertEqual(len(copies), 1, order)
        self.assertLess(order.index("lock"), copies[0])
        script = LAUNCHER.read_text()
        self.assertLess(script.index("controller_ssh_lock shared"), script.index('"${app_env}" "${runtime_app_env}"'))
        self.assertLess(script.index('"${app_env}" "${runtime_app_env}"'), script.index("exec 8>&-"))

    def test_the_launcher_never_sources_or_forwards_connection_values(self):
        script = LAUNCHER.read_text()
        self.assertNotIn('--env "${env_name}"', script)
        self.assertNotIn("set -a", script)
        self.assertNotIn("severino_controller_env", script)
        self.assertIn("controller_require_connections", script)

    def test_an_unsafe_document_stops_the_launch(self):
        for name, change in (
            ("writable", lambda: self.document.chmod(0o600)),
            ("missing", self.document.unlink),
            ("open directory", lambda: self.runtime.chmod(0o755)),
        ):
            with self.subTest(name=name):
                change()
                result = self.launch()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "docker-args").exists())
                self.runtime.chmod(0o700)
                self.write_document()

    def test_a_disk_backed_runtime_stops_the_launch(self):
        self.env["TEST_FILESYSTEM"] = "ext4"
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tmpfs", result.stderr)
        self.assertFalse((self.root / "docker-args").exists())


DOCKERFILE = ROOT / "Dockerfile"
UNIT = ROOT / "deploy/systemd/severino-hq-secrets.service"


# The image is built without its Dockerfile, so these run from a checkout.
@unittest.skipUnless(DOCKERFILE.exists(), "the Dockerfile is not in the image")
class RendererDeliveryTests(unittest.TestCase):
    """Root runs the renderer the signed image built, from the manifested tree."""

    def test_the_unit_runs_the_binary_the_image_ships_in_the_root_tree(self):
        dockerfile = DOCKERFILE.read_text()
        self.assertIn("-o /out/hq-secrets ./cmd/hq-secrets", dockerfile)
        copied = "COPY --from=controller /out/hq-secrets /app/deploy/bin/hq-secrets"
        # After the checkout is copied, so a checkout cannot supply it, and
        # before the manifest is written, so the manifest covers it.
        self.assertLess(dockerfile.index("COPY . /app"), dockerfile.index(copied))
        self.assertLess(dockerfile.index(copied), dockerfile.index("root-tree-manifest.sh /app"))
        started = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(started, ["ExecStart=/usr/local/lib/severino-hq/deploy/bin/hq-secrets"])

    def test_a_locally_built_binary_cannot_enter_the_repository_or_the_image(self):
        for name in (".gitignore", ".dockerignore"):
            with self.subTest(name=name):
                self.assertIn("deploy/bin/", (ROOT / name).read_text().splitlines())

    def test_the_unit_keeps_its_hardening(self):
        unit = UNIT.read_text()
        for directive in (
            "NoNewPrivileges=yes", "PrivateDevices=yes", "PrivateTmp=yes", "ProtectClock=yes",
            "ProtectControlGroups=yes", "ProtectHome=yes", "ProtectHostname=yes", "ProtectKernelLogs=yes",
            "ProtectKernelModules=yes", "ProtectKernelTunables=yes", "ProtectSystem=strict",
            "RestrictNamespaces=yes", "RestrictRealtime=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes", "UMask=0077", "RequiresMountsFor=/run/severino-hq-secrets",
            # Narrower than the shell renderer's.
            "RestrictAddressFamilies=AF_UNIX AF_INET", "IPAddressDeny=any", "IPAddressAllow=127.0.0.0/8",
            "CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE", "SystemCallFilter=@system-service",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, unit.splitlines())
        # No token, vault or endpoint is configured in the shipped unit.
        self.assertNotIn("LoadCredential", unit)
        self.assertNotIn("Environment=", unit)

    def test_the_shell_renderer_is_gone(self):
        for name in ("refresh-secrets.sh", "render-controller-env.sh", "list-secret-items.sh", "render-env.jq"):
            with self.subTest(name=name):
                self.assertFalse((ROOT / "scripts" / name).exists())
