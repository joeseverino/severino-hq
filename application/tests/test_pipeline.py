"""The release pipeline's structure: what runs where, and what may publish."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

ROOT = Path(__file__).parent.resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def jobs(text: str) -> dict[str, str]:
    """Each job's text, by id, read by indentation under ``jobs:``."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    found: dict[str, str] = {}
    name = None
    for line in body.splitlines(keepends=True):
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            name = match.group(1)
            found[name] = ""
        elif name:
            found[name] += line
    return found


def steps(job: str) -> list[str]:
    """Each step's text, in order, split at its ``- name:`` or ``- uses:``."""
    return [
        "- " + part
        for part in re.split(r"(?m)^      - ", job)[1:]
    ]


def condition(step: str) -> str:
    """A step's ``if:`` expression, folded onto one line; empty when it has none."""
    lines = step.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)if:\s*(.*)$", line)
        if not match or len(match.group(1)) != 8:
            continue
        value = match.group(2)
        if value not in {">-", ">", "|"}:
            return value
        folded = []
        for rest in lines[index + 1:]:
            if len(rest) - len(rest.lstrip()) <= 8:
                break
            folded.append(rest.strip())
        return " ".join(folded)
    return ""


def named(steps_: list[str], name: str) -> int:
    for index, step in enumerate(steps_):
        if step.startswith(f"- name: {name}\n"):
            return index
    raise AssertionError(f"no step named {name!r}")


class DeployCheckoutOwnershipTests(SimpleTestCase):
    def test_the_deploy_refuses_a_foreign_owned_checkout_before_it_pulls(self):
        deploy = steps(jobs((WORKFLOWS / "deploy.yml").read_text())["deploy"])
        refuse = named(deploy, "Refuse a checkout the runner cannot pull")
        pull = named(deploy, "Sync deploy checkout")
        self.assertLess(refuse, pull)
        self.assertIn("pull --ff-only", deploy[pull])
        self.assertIn('! -user "$runner"', deploy[refuse])
        self.assertIn("sudo chown -R", deploy[refuse])


class CoordinatedBranchTests(SimpleTestCase):
    """A coordinated change is verified together and can never be published."""

    def setUp(self):
        self.compose_job = jobs((WORKFLOWS / "compose.yml").read_text())["compose"]
        self.deploy_job = jobs((WORKFLOWS / "deploy.yml").read_text())["deploy"]
        self.steps = steps(self.compose_job)

    def test_every_candidate_step_runs_for_a_pull_request_only(self):
        candidate = [
            step for step in self.steps
            if "steps.coordinated.outputs.found == 'true'" in condition(step)
        ]
        self.assertGreaterEqual(len(candidate), 3)
        for step in candidate:
            with self.subTest(step=step.splitlines()[0]):
                self.assertIn("github.event_name == 'pull_request' &&", condition(step))
        finder = self.steps[named(self.steps, "Find coordinated extension branches")]
        self.assertEqual(condition(finder), "github.event_name == 'pull_request'")

    def test_the_candidate_image_has_no_registry_and_nothing_pushes_it(self):
        build = self.steps[named(self.steps, "Build the candidate image")]
        self.assertIn('-t "severino-hq-candidate:$GITHUB_SHA"', build)
        self.assertIn("SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION=0", build)
        for step in self.steps:
            if "docker push" in step or "cosign sign" in step:
                with self.subTest(step=step.splitlines()[0]):
                    self.assertNotIn("candidate", step)
                    self.assertIn("env.IS_RELEASE == 'true'", step)

    def test_branch_code_builds_without_the_jobs_credentials(self):
        build = self.steps[named(self.steps, "Build coordinated extension branches")]
        run = build.split("run: |", 1)[1]
        self.assertIn('rm -rf "$dir/.git"', run)
        self.assertIn('docker build -f composition/Dockerfile --target installer-base', run)
        self.assertIn('--entrypoint uv "severino-hq-installer:$GITHUB_SHA"', run)
        self.assertIn('build --python /usr/local/bin/python --no-cache --wheel', run)
        self.assertNotIn("--env GH_TOKEN", run)
        self.assertNotIn("docker.sock", run)
        self.assertNotIn("pip wheel", run)

    def test_the_admitted_build_is_skipped_for_a_coordinated_change(self):
        for name in ("Merge locks and stage the build context", "Build composed image"):
            with self.subTest(name=name):
                self.assertIn(
                    "steps.coordinated.outputs.found != 'true'",
                    self.steps[named(self.steps, name)],
                )

    def test_candidates_are_built_on_hosted_runners_and_never_deployed(self):
        self.assertEqual(re.findall(r"(?m)^    runs-on: (.*)$", self.compose_job), ["ubuntu-24.04"])
        self.assertNotIn("coordinated", self.deploy_job)
        self.assertNotIn("candidate", self.deploy_job)

    def test_an_extension_checks_against_its_coordinated_host_branch(self):
        checks = (WORKFLOWS / "plugin-checks.yml").read_text()
        test = steps(jobs(checks)["test"])
        resolve = test[named(test, "Resolve the host ref")]
        checkout = test[named(test, "Check out HQ contract")]
        self.assertLess(named(test, "Resolve the host ref"), named(test, "Check out HQ contract"))
        self.assertIn("ref: ${{ steps.hq.outputs.ref }}", checkout)
        self.assertIn("git ls-remote --exit-code --heads", resolve)
        self.assertRegex(checks, r'hq-ref:\n(?:.*\n)*?        default: ""\n')

    def test_host_commit_pinning_is_retired(self):
        tracked = [
            path for path in (ROOT / ".github").rglob("*")
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        ] + [ROOT / "docs" / "PLUGINS.md", ROOT / "README.md", ROOT / "AGENTS.md"]
        # The image ships no docs; the workflows are the part that must hold.
        offenders = [
            str(path.relative_to(ROOT)) for path in tracked
            if path.exists() and re.search(r"HQ_COMMIT|hq-commit", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "a coordinated branch replaces the host pin")


def compose_plugins():
    spec = importlib.util.spec_from_file_location(
        "compose_plugins", ROOT / "scripts" / "compose-plugins.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComposeCandidateTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.module = compose_plugins()

    def wheel(self, name: str) -> Path:
        path = self.root / "in" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(name.encode())
        return path

    def run_main(self, *argv: str) -> tuple[int, str]:
        output = self.root / "github-output"
        output.write_text("")
        with (
            patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = self.module.main(list(argv))
        return code, output.read_text()

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
        code, output = self.run_main(
            "--entry", self.entry(), "--wheel", str(alpha), "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((out / "plugin-lock.json").read_text())["plugins"][0]
                         ["distribution"], "example-alpha")
        self.assertIn("references=example_alpha.plugin:plugin\n", output)
        self.assertIn(f"policy_sha256={'a' * 64}\n", output)

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
                self.assertEqual(output, "")
        self.cordon([self.admitted(alpha), self.admitted(beta, policy="b" * 64)])
        code, _ = self.run_main(
            "--entry", self.entry(), "--wheel", str(alpha),
            "--entry", self.entry(), "--wheel", str(beta), "--out", str(self.root / "out"),
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
        self.assertEqual(output, "")

    def test_a_candidate_stages_wheels_with_no_lock(self):
        out = self.root / "out"
        out.mkdir()
        (out / "plugin-lock.json").write_text("{}")
        beta = self.wheel("example_beta-1.0-py3-none-any.whl")
        alpha = self.wheel("example_alpha-2.0-py3-none-any.whl")
        code, output = self.run_main(
            "--candidate", "--wheel", str(beta), "--wheel", str(alpha), "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertFalse((out / "plugin-lock.json").exists())
        self.assertEqual(sorted(path.name for path in out.glob("*.whl")),
                         [alpha.name, beta.name])
        self.assertIn(
            "references=example_alpha.plugin:plugin,example_beta.plugin:plugin\n", output
        )
        self.assertIn(f"{self.module.sha256(alpha)}  {alpha.name}", output)
        self.assertNotIn("policy_sha256", output)

    def test_a_distribution_twice_is_refused(self):
        first = self.wheel("example_alpha-1.0-py3-none-any.whl")
        second = self.wheel("example_alpha-2.0-py3-none-any.whl")
        code, _ = self.run_main(
            "--candidate", "--wheel", str(first), "--wheel", str(second),
            "--out", str(self.root / "out"),
        )
        self.assertEqual(code, 1)

    def test_a_candidate_takes_no_admission_entries(self):
        wheel = self.wheel("example_alpha-1.0-py3-none-any.whl")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.module.main([
                "--candidate", "--entry", str(self.root / "entry.json"),
                "--wheel", str(wheel), "--out", str(self.root / "out"),
            ])
