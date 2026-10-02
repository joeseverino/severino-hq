"""The release pipeline's structure: what runs where, and what may publish."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
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
    def test_a_checkout_git_cannot_pull_warns_and_never_holds_the_release(self):
        deploy = steps(jobs((WORKFLOWS / "deploy.yml").read_text())["deploy"])
        sync = deploy[named(deploy, "Sync the deploy checkout")]
        self.assertIn("set -uo pipefail", sync)
        self.assertIn('! -user "$(id -un)"', sync)
        self.assertIn("elif ! git -C", sync)
        self.assertNotIn("::error", sync)
        self.assertLess(
            named(deploy, "Sync the deploy checkout"),
            named(deploy, "Deploy exact image with health rollback"),
        )


class PromotionTests(SimpleTestCase):
    """A push to main stands on its pull request's run only with proof."""

    def setUp(self):
        self.jobs = jobs((WORKFLOWS / "ci.yml").read_text())

    def test_proof_is_sought_on_a_push_and_its_failure_runs_every_gate(self):
        proven = self.jobs["proven"]
        self.assertIn("if: github.event_name == 'push'", proven)
        self.assertIn("continue-on-error: true", proven)
        self.assertIn("scripts/proven-on-pr.sh", proven)

    def test_every_gate_runs_unless_the_tree_was_proven(self):
        for job in ("checks", "tests", "browser"):
            with self.subTest(job=job):
                self.assertIn(
                    "if: ${{ !cancelled() && needs.proven.outputs.digest == '' }}", self.jobs[job]
                )

    def test_a_promoted_image_is_never_built_scanned_or_pushed_again_but_is_signed(self):
        image = steps(self.jobs["image"])
        for step in image:
            first = step.splitlines()[0]
            with self.subTest(step=first):
                if any(word in step for word in ("buildx build", "trivy-action", "docker push")):
                    self.assertIn("if: env.PROVEN == ''", step)
        promote = image[named(image, "Promote the image its pull request proved")]
        self.assertIn("if: env.PROVEN != ''", promote)
        sign = image[named(image, "Sign")]
        self.assertNotIn("if:", sign)
        self.assertIn("steps.promote.outputs.digest", sign)

    def test_the_image_records_the_tree_it_was_built_from(self):
        self.assertIn('--label "dev.severino.hq.tree=$(git rev-parse \'HEAD^{tree}\')"',
                      self.jobs["image"])


class PublicLogTests(SimpleTestCase):
    """What a composed build prints is safe to publish in a public log."""

    def setUp(self):
        self.steps = steps(jobs((WORKFLOWS / "compose.yml").read_text())["compose"])

    def test_the_composed_suite_prints_through_the_filter(self):
        suite = self.steps[named(self.steps, "Verify the composition as one application")]
        self.assertIn('scripts/composed-suite.sh "$IMAGE"', suite)
        self.assertNotIn("manage.py test", suite)

    def test_the_scan_writes_a_file_not_the_package_table(self):
        scan = self.steps[named(self.steps, "Scan composed image")]
        self.assertIn("format: json", scan)
        self.assertIn("output: trivy-composed.json", scan)

    def test_a_coordinated_build_keeps_its_output_off_the_log(self):
        build = self.steps[named(self.steps, "Build coordinated extension branches")]
        self.assertIn('> "$WITHHELD_DIR/candidate-build-', build)

    def test_withheld_output_leaves_the_runner_only_sealed(self):
        uploads = [step for step in self.steps if "actions/upload-artifact@" in step]
        self.assertEqual(len(uploads), 1)
        self.assertIn("path: failure-logs.tar.age", uploads[0])
        seal = self.steps[named(self.steps, "Seal the withheld output")]
        self.assertIn('scripts/seal-failure-logs.sh "$WITHHELD_DIR" failure-logs.tar.age', seal)
        self.assertLess(named(self.steps, "Seal the withheld output"),
                        named(self.steps, "Upload the sealed output"))

    def test_wheel_digests_reach_the_build_as_a_file_not_an_argument(self):
        # The build log prints each RUN with its build arguments expanded.
        for path in (WORKFLOWS / "compose.yml", ROOT / "composition" / "Dockerfile"):
            with self.subTest(path=path.name):
                self.assertNotIn("PLUGIN_WHEEL_DIGESTS", path.read_text())
        self.assertIn("build/composition/digests /tmp/plugin/",
                      (ROOT / "composition" / "Dockerfile").read_text())

    def test_sealed_logs_are_encrypted_to_a_post_quantum_key(self):
        pins = (ROOT / "scripts" / "toolchain.env").read_text()
        self.assertRegex(pins, r"(?m)^FAILURE_LOG_RECIPIENT=age1pq1[0-9a-z]+$")

    def test_extension_build_arguments_never_pass_through_a_step_env(self):
        for step in self.steps:
            with self.subTest(step=step.splitlines()[0]):
                for name in ("DIGESTS", "REFERENCES", "ARGS"):
                    self.assertNotRegex(step, rf"\n\s+\w*{name}: \$\{{\{{")

    def test_every_spelling_of_an_extension_is_masked_in_every_case(self):
        mask = self.steps[named(self.steps, "Mask the extension names for the rest of the job")]
        for spelling in ("${spelling}", "${spelling^}", "${spelling^^}"):
            with self.subTest(spelling=spelling):
                self.assertIn(f"::add-mask::{spelling}", mask)


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
        code, output = self.run_main(
            "--entry", self.entry(), "--wheel", str(alpha), "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((out / "plugin-lock.json").read_text())["plugins"][0]
                         ["distribution"], "example-alpha")
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
        self.assertEqual(output, {})

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
            "example_alpha.plugin:plugin,example_beta.plugin:plugin\n", output["references"]
        )
        self.assertIn(f"{self.module.sha256(alpha)}  {alpha.name}", output["digests"])
        self.assertNotIn("policy-sha256", output)

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


class ScriptInputTests(SimpleTestCase):
    """What a script is handed arrives as data, never as its text."""

    def run_blocks(self):
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            lines = workflow.read_text().splitlines()
            index = 0
            while index < len(lines):
                match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[index])
                if not match:
                    index += 1
                    continue
                indent, block = len(match.group(1)), [match.group(2)]
                index += 1
                while index < len(lines) and (
                    not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent
                ):
                    block.append(lines[index])
                    index += 1
                yield workflow.name, "\n".join(block)

    def test_no_script_interpolates_a_step_output_or_the_event(self):
        """A step output can carry a file name an extension chose, and the event
        a title anyone can type. Interpolated, either is script text; through
        env it stays a value. GitHub's own fixed fields stay allowed."""

        blocks = list(self.run_blocks())
        self.assertGreater(len(blocks), 20)
        for name, block in blocks:
            for expression in re.findall(r"\$\{\{\s*([^}]*?)\s*\}\}", block):
                with self.subTest(workflow=name, expression=expression):
                    self.assertFalse(
                        re.match(r"(steps|needs|inputs)\.|github\.event\.|github\.head_ref", expression),
                        f"{name} interpolates {expression} into a script; pass it through env",
                    )

    def test_an_extension_artifact_must_be_named_like_a_wheel_before_it_is_used(self):
        compose = (WORKFLOWS / "compose.yml").read_text()
        collect = compose.index('wheel=$(find "$dir" -name')
        check = compose.index("ships a wheel whose name is not a wheel's")
        use = compose.index('--artifact "$wheel"')
        self.assertLess(collect, check)
        self.assertLess(check, use)


def plugin_identity():
    spec = importlib.util.spec_from_file_location(
        "plugin_identity", ROOT / "scripts" / "plugin-identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginIdentityTests(SimpleTestCase):
    """An extension's identity is read from its package, and only from there."""

    def package(self, manifest: str, name: str = "example-alpha") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "1.0"\n')
        source = root / "src" / "example_alpha"
        source.mkdir(parents=True)
        (source / "plugin.py").write_text(manifest)
        return root

    def manifest(self, **fields: str) -> str:
        declared = {
            "id": '"example.alpha"',
            "distribution": '"example-alpha"',
            "django_apps": '("example_alpha",)',
        } | fields
        body = ", ".join(f"{key}={value}" for key, value in declared.items())
        return f"from hq_sdk.plugin import PluginManifest\nplugin = PluginManifest({body})\n"

    def test_the_package_declares_everything_admission_needs(self):
        found = plugin_identity().identity(self.package(self.manifest()))
        self.assertEqual(found, {
            "distribution": "example-alpha",
            "plugin-id": "example.alpha",
            "plugin-reference": "example_alpha.plugin:plugin",
            "django-app": "example_alpha",
        })

    def test_a_package_that_disagrees_with_itself_is_refused(self):
        cases = {
            "distribution": self.manifest(distribution='"example-beta"'),
            "django_apps": self.manifest(django_apps='("other",)'),
            "a literal": self.manifest(id="IDENTIFIER"),
            "no module-level": "plugin = make()\n",
        }
        for reason, manifest in cases.items():
            with self.subTest(reason=reason), self.assertRaises(SystemExit) as refused:
                plugin_identity().identity(self.package(manifest))
            self.assertIn(reason, str(refused.exception))

    def test_a_manifest_away_from_its_package_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            plugin_identity().identity(self.package(self.manifest(), name="example-gamma"))
        self.assertIn("src/example_gamma/plugin.py", str(refused.exception))


class ExtensionCallerTests(SimpleTestCase):
    """An extension's caller names nothing about the extension."""

    def test_the_reusable_checks_and_admission_take_no_identity(self):
        checks = (WORKFLOWS / "plugin-checks.yml").read_text()
        admit = (ROOT / ".github" / "actions" / "admit-plugin" / "action.yml").read_text()
        for name in ("plugin-reference:", "django-app:", "plugin-id:", "distribution:"):
            with self.subTest(name=name):
                self.assertNotIn(f"\n  {name}", admit.split("\nruns:")[0])
                self.assertNotIn(f"\n      {name}", checks.split("\npermissions:")[0])
        self.assertIn("check-plugin.sh --plugin-root .", checks)
        self.assertIn("scripts/plugin-identity.py", admit)


class ActionPinTests(SimpleTestCase):
    def test_every_action_has_one_version_across_workflows_and_composite_actions(self):
        files = [*WORKFLOWS.glob("*.yml"), *(ROOT / ".github" / "actions").glob("*/action.yml")]
        pins: dict[str, set[str]] = {}
        for path in files:
            for action, sha in re.findall(r"uses: ([\w./-]+)@([0-9a-f]{40})", path.read_text()):
                pins.setdefault(action, set()).add(sha)
        self.assertGreater(len(pins), 5)
        self.assertEqual({action: shas for action, shas in pins.items() if len(shas) > 1}, {})
