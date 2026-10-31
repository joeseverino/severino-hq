"""Compose verifies every admission against the signer its set declares before anything is composed."""

import hashlib
import json
import subprocess
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.pipeline import verify_admissions

POLICY = b'{"version": 1}\n'
BASE_POLICY = b'{"version": 0}\n'
WORKFLOW = ".github/workflows/admit-plugin.yml"
WHEEL = "example_alpha-1.0-py3-none-any.whl"


class FakeRun:
    """git and node as Compose runs them: node writes an entry unless told to fail."""

    def __init__(self, *, node_fails: bool = False):
        self.node_fails = node_fails
        self.calls: list[list[str]] = []

    def __call__(self, args, check, stdout=None):
        self.calls.append(list(args))
        if args[0] == "node":
            if self.node_fails:
                raise subprocess.CalledProcessError(1, args)
            stdout.write(b'{"verified": true}\n')
        shown = BASE_POLICY if args[:2] == ["git", "show"] else b""
        return subprocess.CompletedProcess(args, 0, stdout=shown)

    def node(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "node"]


def flag(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


class VerifyAdmissionsTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "policy.json").write_bytes(POLICY)
        self.extensions = []

    def extension(self, plugin_id, repository, policy=POLICY, wheel=WHEEL, files=("statement", "bundle", "wheel")):
        self.extensions.append({"id": plugin_id, "repository": repository, "workflow": WORKFLOW})
        directory = self.root / "artifacts" / plugin_id / "nested"
        directory.mkdir(parents=True)
        if "statement" in files:
            statement = {"policy": {"sha256": hashlib.sha256(policy).hexdigest()}}
            (directory / "admission.json").write_text(json.dumps(statement))
        if "bundle" in files:
            (directory / "admission.sigstore.json").write_text("{}")
        if "wheel" in files:
            (directory / wheel).write_bytes(b"wheel")

    def verify(self, run, *, candidate=False, coordinated=""):
        (self.root / "set.json").write_text(json.dumps({"extensions": self.extensions}))
        (self.root / "coordinated.txt").write_text(coordinated)
        paths = {
            "--policy": "policy.json",
            "--set": "set.json",
            "--coordinated": "coordinated.txt",
            "--artifacts": "artifacts",
            "--cordon": "cordon",
            "--out": "build/compose-args",
            "--candidate-wheels": "candidate-wheels",
        }
        argv = [part for name, path in paths.items() for part in (name, str(self.root / path))]
        env = {"CANDIDATE": "true" if candidate else "false", "BASE_SHA": "c" * 40}
        printed = StringIO()
        with mock.patch.dict("os.environ", env), redirect_stdout(printed):
            status = verify_admissions.main(argv, run=run)
        return status, printed.getvalue()

    def compose_args(self) -> list[str]:
        return (self.root / "build" / "compose-args").read_text().split()

    def test_each_admission_is_verified_against_the_signer_the_set_declares(self):
        self.extension("example.alpha", "example/alpha")
        self.extension("example.beta", "example/extensions")
        run = FakeRun()
        status, _ = self.verify(run)
        self.assertEqual(status, 0)
        policy_sha = hashlib.sha256(POLICY).hexdigest()
        identities = [flag(call, "--identity") for call in run.node()]
        self.assertEqual(
            identities,
            [
                f"https://github.com/{repo}/{WORKFLOW}@refs/heads/main"
                for repo in ("example/alpha", "example/extensions")
            ],
        )
        self.assertTrue(all(flag(call, "--policy-sha") == policy_sha for call in run.node()))
        args = self.compose_args()
        self.assertEqual(args[:2], ["--policy-sha256", policy_sha])
        self.assertEqual(args.count("--entry"), 2)
        for entry in args[3::4]:
            self.assertTrue(Path(entry).read_text())
        self.assertTrue(all(Path(wheel).name == WHEEL for wheel in args[5::4]))

    def test_a_composition_of_no_extensions_still_states_its_policy(self):
        status, _ = self.verify(FakeRun())
        self.assertEqual(status, 0)
        self.assertEqual(self.compose_args(), ["--policy-sha256", hashlib.sha256(POLICY).hexdigest()])

    def test_an_admission_that_cannot_be_composed_is_refused_before_it_is_verified(self):
        cases = {
            "missing its admission statement, bundle or wheel": {"files": ("statement", "wheel")},
            "whose name is not a wheel's": {"wheel": "example_alpha.whl"},
            "whose name is not a plain file name": {"wheel": "example$alpha-1.0-py3-none-any.whl"},
            "was admitted under policy": {"policy": b"another policy"},
        }
        for reason, shape in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                self.extension("example.alpha", "example/alpha", **shape)
                run = FakeRun()
                status, printed = self.verify(run)
                self.assertEqual(status, 1)
                self.assertIn("::error::", printed)
                self.assertIn(reason, printed)
                self.assertEqual(run.node(), [])
                self.assertFalse((self.root / "build" / "compose-args").exists())

    def test_a_policy_mismatch_names_both_policies(self):
        self.extension("example.alpha", "example/alpha", policy=b"another policy")
        _, printed = self.verify(FakeRun())
        self.assertIn(hashlib.sha256(b"another policy").hexdigest(), printed)
        self.assertIn(hashlib.sha256(POLICY).hexdigest(), printed)

    def test_a_signature_that_does_not_verify_stops_the_composition(self):
        self.extension("example.alpha", "example/alpha")
        status, _ = self.verify(FakeRun(node_fails=True))
        self.assertNotEqual(status, 0)
        self.assertFalse((self.root / "build" / "compose-args").exists())

    def test_a_candidate_verifies_the_rest_under_the_base_policy_and_keeps_their_wheels(self):
        self.extension("example.alpha", "example/alpha", policy=BASE_POLICY)
        self.extension("example.beta", "example/extensions")
        run = FakeRun()
        status, _ = self.verify(run, candidate=True, coordinated="example/extensions=" + "d" * 40 + "\n")
        self.assertEqual(status, 0)
        self.assertIn(["git", "fetch", "--quiet", "--depth", "1", "origin", "c" * 40], run.calls)
        self.assertEqual([flag(call, "--repository") for call in run.node()], ["example/alpha"])
        self.assertEqual(self.compose_args()[1], hashlib.sha256(BASE_POLICY).hexdigest())
        self.assertEqual([path.name for path in (self.root / "candidate-wheels").iterdir()], [WHEEL])
