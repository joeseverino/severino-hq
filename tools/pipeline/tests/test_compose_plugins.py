"""compose_plugins stages the candidate's wheels and writes its build arguments."""

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from tools.pipeline import compose_plugins


class ComposeCandidateTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.module = compose_plugins

    def wheel(self, name: str) -> Path:
        path = self.root / "in" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(name.encode())
        return path

    def run_main(self, *argv: str) -> tuple[int, dict[str, str]]:
        """The exit code and the build arguments written beside the wheels."""
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.module.main(list(argv))
        out = Path(argv[argv.index("--out") + 1])
        written = {
            name: (out / name).read_text()
            for name in ("references", "digests", "policy-sha256")
            if (out / name).exists()
        }
        return code, written

    def cordon(self, plugins: list[dict]) -> None:
        """A stand-in for Cordon's lock tool that merges into ``plugins``."""
        tool = self.root / "cordon-lock"
        tool.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps({'plugins': plugins})}\nEOF\n")
        tool.chmod(0o755)
        patcher = patch.object(self.module, "CORDON_LOCK", str(tool))
        patcher.start()
        self.addCleanup(patcher.stop)

    def admitted(self, wheel: Path, policy: str = "a" * 64, digest: str | None = None) -> dict:
        return {
            "distribution": self.module.distribution_of(wheel),
            "artifact_sha256": digest or self.module.sha256(wheel),
            "policy_sha256": policy,
        }

    def entry(self) -> str:
        path = self.root / "entry.json"
        path.write_text("{}")
        return str(path)

    def test_an_admitted_composition_writes_its_lock_and_references(self):
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        self.cordon([self.admitted(alpha)])
        out = self.root / "out"
        code, output = self.run_main("--entry", self.entry(), "--wheel", str(alpha), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads((out / "plugin-lock.json").read_text())["plugins"][0]["distribution"], "example-alpha"
        )
        self.assertEqual(output["references"], "example_alpha.plugin:plugin\n")
        self.assertEqual(output["policy-sha256"], f"{'a' * 64}\n")

    def test_an_admitted_composition_refuses_what_its_lock_does_not_approve(self):
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        beta = self.wheel("example_beta-1.0-py3-none-any.whl")
        cases = {
            "a wheel with no entry": [self.admitted(beta)],
            "a wheel that is not the admitted artifact": [self.admitted(alpha, digest="0" * 64)],
        }
        for why, plugins in cases.items():
            with self.subTest(why=why):
                self.cordon(plugins)
                code, output = self.run_main(
                    "--entry", self.entry(), "--wheel", str(alpha), "--out", str(self.root / "out")
                )
                self.assertEqual(code, 1)
                self.assertEqual(output, {})
        self.cordon([self.admitted(alpha), self.admitted(beta, policy="b" * 64)])
        code, _ = self.run_main(
            "--entry",
            self.entry(),
            "--wheel",
            str(alpha),
            "--entry",
            self.entry(),
            "--wheel",
            str(beta),
            "--out",
            str(self.root / "out"),
        )
        self.assertEqual(code, 1, "two admission policies cannot satisfy one runtime")

    def test_a_refused_merge_stops_the_composition(self):
        tool = self.root / "cordon-refuses"
        tool.write_text("#!/bin/sh\necho 'duplicate plugin id' >&2\nexit 1\n")
        tool.chmod(0o755)
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        with patch.object(self.module, "CORDON_LOCK", str(tool)):
            code, output = self.run_main(
                "--entry", self.entry(), "--wheel", str(alpha), "--out", str(self.root / "out")
            )
        self.assertEqual(code, 1)
        self.assertEqual(output, {})

    def test_a_composition_of_no_extensions_is_the_host_alone(self):
        out = self.root / "out"
        code, output = self.run_main("--policy-sha256", "a" * 64, "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads((out / "plugin-lock.json").read_text()),
            {"schema_version": 1, "host": "severino-hq", "plugins": []},
        )
        self.assertEqual(output["references"], "\n")
        self.assertEqual(output["policy-sha256"], f"{'a' * 64}\n")
        self.assertEqual(list(out.glob("*.whl")), [])

    def test_a_composition_of_no_extensions_must_state_its_policy(self):
        for argv in ((), ("--policy-sha256", "not-a-digest")):
            with self.subTest(argv=argv):
                code, output = self.run_main(*argv, "--out", str(self.root / "out"))
                self.assertEqual(code, 1)
                self.assertEqual(output, {})

    def test_an_admission_under_another_policy_than_the_compositions_is_refused(self):
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        self.cordon([self.admitted(alpha, policy="b" * 64)])
        code, output = self.run_main(
            "--policy-sha256", "a" * 64, "--entry", self.entry(), "--wheel", str(alpha), "--out", str(self.root / "out")
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, {})

    def test_a_candidate_stages_wheels_with_no_lock(self):
        out = self.root / "out"
        out.mkdir()
        (out / "plugin-lock.json").write_text("{}")
        beta = self.wheel("example_beta-1.0-py3-none-any.whl")
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        code, output = self.run_main("--candidate", "--wheel", str(beta), "--wheel", str(alpha), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertFalse((out / "plugin-lock.json").exists())
        self.assertEqual(sorted(path.name for path in out.glob("*.whl")), [alpha.name, beta.name])
        self.assertIn("example_alpha.plugin:plugin,example_beta.plugin:plugin\n", output["references"])
        self.assertIn(f"{self.module.sha256(alpha)}  {alpha.name}", output["digests"])
        self.assertNotIn("policy-sha256", output)

    def test_a_distribution_twice_is_refused(self):
        first = self.wheel("example_alpha-1.0-py3-none-any.whl")
        second = self.wheel("example_alpha-2.0-py3-none-any.whl")
        code, _ = self.run_main(
            "--candidate",
            "--wheel",
            str(first),
            "--wheel",
            str(second),
            "--out",
            str(self.root / "out"),
        )
        self.assertEqual(code, 1)

    def test_a_candidate_takes_no_admission_entries(self):
        wheel = self.wheel("example_alpha-1.0-py3-none-any.whl")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.module.main(
                [
                    "--candidate",
                    "--entry",
                    str(self.root / "entry.json"),
                    "--wheel",
                    str(wheel),
                    "--out",
                    str(self.root / "out"),
                ]
            )
