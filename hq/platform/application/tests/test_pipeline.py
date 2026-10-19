"""What the pipeline promises: pinned actions, least privilege, sealed logs, and releases only from main."""

import importlib.util
import io
import json
import re
import tempfile
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]


def workflows() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text()


def composite_actions() -> list[Path]:
    return sorted((ROOT / ".github" / "actions").glob("*/action.yml"))


def jobs(text: str) -> dict[str, str]:
    """Each job's text, by id, read by indentation under ``jobs:``."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
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
    return ["- " + part for part in re.split(r"(?m)^      - ", job)[1:]]


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
        for rest in lines[index + 1 :]:
            if len(rest) - len(rest.lstrip()) <= 8:
                break
            folded.append(rest.strip())
        return " ".join(folded)
    return ""


def block(text: str, key: str, indent: int) -> list[str]:
    """The value of ``key`` at ``indent``: its inline value, or the lines nested under it."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(rf"^ {{{indent}}}{key}:\s*(.*?)\s*(?:#.*)?$", line)
        if not match:
            continue
        if match.group(1) and match.group(1) not in {">-", ">", "|"}:
            return [match.group(1)]
        nested = []
        for rest in lines[index + 1 :]:
            if rest.strip() and len(rest) - len(rest.lstrip()) <= indent:
                break
            if rest.strip() and not rest.strip().startswith("#"):
                nested.append(rest.strip())
        return nested
    return []


def needed(job: str) -> set[str]:
    """The job ids a job's ``needs:`` names, inline, as a list, or nested."""
    return {name for part in block(job, "needs", 4) for name in re.findall(r"[A-Za-z0-9_-]+", part.removeprefix("- "))}


def upstream(name: str, needs: dict[str, set[str]]) -> set[str]:
    """Every job ``name`` waits on, directly or through another."""
    seen: set[str] = set()
    pending = list(needs.get(name, ()))
    while pending:
        job = pending.pop()
        if job not in seen:
            seen.add(job)
            pending.extend(needs.get(job, ()))
    return seen


def uses(text: str) -> list[str]:
    return re.findall(r"(?m)^\s*(?:- )?uses:\s*([^\s#]+)", text)


def code(text: str) -> str:
    """The text without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


@tag("source")
class ActionPinTests(SimpleTestCase):
    def test_every_action_is_local_or_pinned_to_a_commit(self):
        found = [
            (path.name, action) for path in [*workflows(), *composite_actions()] for action in uses(path.read_text())
        ]
        self.assertGreater(len(found), 10)
        for name, action in found:
            with self.subTest(file=name, action=action):
                self.assertRegex(action, r"^(\./.+|[\w.-]+/[\w./-]+@[0-9a-f]{40}|docker://.+@sha256:[0-9a-f]{64})$")

    def test_every_action_has_one_version_across_workflows_and_composite_actions(self):
        pins: dict[str, set[str]] = {}
        for path in [*workflows(), *composite_actions()]:
            for action, sha in re.findall(r"uses: ([\w./-]+)@([0-9a-f]{40})", path.read_text()):
                pins.setdefault(action, set()).add(sha)
        self.assertGreater(len(pins), 5)
        self.assertEqual({action: shas for action, shas in pins.items() if len(shas) > 1}, {})


@tag("source")
class JobConditionTests(SimpleTestCase):
    def test_a_job_after_a_skippable_job_names_its_status_check(self):
        """A bare ``if:`` also requires every job upstream to have succeeded, so one
        skipped by its own condition (a push-only job) silently skips this one too."""
        checked = 0
        for path in workflows():
            found = jobs(path.read_text())
            needs = {name: needed(job) for name, job in found.items()}
            skippable = {name for name, job in found.items() if block(job, "if", 4)}
            for name, job in found.items():
                expression = " ".join(block(job, "if", 4))
                if not expression or re.search(r"\b(always|cancelled|success|failure)\(\)", expression):
                    continue
                checked += 1
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual(upstream(name, needs) & skippable, set())
        self.assertGreater(checked, 1)


@tag("source")
class LeastPrivilegeTests(SimpleTestCase):
    def test_no_workflow_grants_a_write_to_every_job(self):
        for path in workflows():
            with self.subTest(workflow=path.name):
                granted = block(path.read_text(), "permissions", 0)
                self.assertTrue(granted, "a workflow states its default permissions")
                self.assertTrue(
                    granted in (["read-all"], ["{}"]) or all(re.fullmatch(r"[\w-]+: (read|none)", g) for g in granted),
                    granted,
                )

    def test_no_workflow_runs_a_pull_requests_code_with_the_repositorys_secrets(self):
        for path in workflows():
            with self.subTest(workflow=path.name):
                self.assertNotIn("pull_request_target", code(path.read_text()))

    def test_only_a_job_that_signs_or_publishes_holds_an_identity_token(self):
        # The actions whose only work is to present the job's OIDC identity.
        presenters = ("actions/deploy-pages@", "ossf/scorecard-action@", "./.github/actions/cosign")
        holders = 0
        for path in workflows():
            for name, job in jobs(path.read_text()).items():
                if "id-token: write" not in code(job):
                    continue
                holders += 1
                called = [
                    (ROOT / action).read_text() for action in uses(job) if action.startswith("./.github/workflows/")
                ]
                with self.subTest(workflow=path.name, job=name):
                    self.assertTrue(
                        "cosign sign" in job
                        or any(action.startswith(presenters) for action in uses(job))
                        or any("cosign sign" in text for text in called)
                    )
        self.assertGreater(holders, 0)


@tag("source")
class RenovateTests(SimpleTestCase):
    def test_every_command_the_configuration_runs_is_one_the_workflow_allows(self):
        """Renovate skips a post-upgrade command its runner does not allow, and the
        pull request then carries a version bump without what it needed."""
        config = json.loads((ROOT / ".github" / "renovate.json").read_text())
        commands = [
            command
            for rule in config["packageRules"]
            for command in rule.get("postUpgradeTasks", {}).get("commands", [])
        ]
        allowed = json.loads(
            re.search(r"(?m)^\s+RENOVATE_ALLOWED_COMMANDS: '(.*)'$", workflow("renovate.yml"))
            .group(1)
            .replace("''", "'")
        )
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(any(re.search(pattern, command) for pattern in allowed))


@tag("source")
class ReleaseTests(SimpleTestCase):
    """Only a dispatch on main publishes and signs the image production runs."""

    def setUp(self):
        self.compose = workflow("compose.yml")

    def test_a_release_is_a_dispatch_on_main_and_nothing_else(self):
        defined = re.search(r"(?m)^  IS_RELEASE: \$\{\{(.*)\}\}$", self.compose)
        self.assertIsNotNone(defined)
        terms = {term.strip() for term in defined.group(1).split("&&")}
        self.assertEqual(terms, {"github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'"})

    def test_the_composition_is_pushed_and_signed_only_in_a_release(self):
        publishing = [
            step for step in steps(jobs(self.compose)["compose"]) if re.search(r"docker push|cosign sign\b", code(step))
        ]
        self.assertGreaterEqual(len(publishing), 2)
        for step in publishing:
            with self.subTest(step=step.splitlines()[0]):
                gate = condition(step)
                self.assertTrue(gate == "env.IS_RELEASE == 'true'" or gate.startswith("env.IS_RELEASE == 'true' &&"))
                self.assertNotIn("candidate", step)

    def test_branch_code_builds_without_the_jobs_credentials(self):
        clones = [step for step in steps(jobs(self.compose)["compose"]) if "gh repo clone" in step]
        self.assertTrue(clones)
        for step in clones:
            run = step.split("run: |", 1)[1]
            with self.subTest(step=step.splitlines()[0]):
                self.assertIn('rm -rf "$dir/.git"', run)
                self.assertIn('"$WITHHELD_DIR/', run)
                for absent in ("--env GH_TOKEN", "docker.sock", "pip wheel"):
                    self.assertNotIn(absent, run)


@tag("source")
class ExtensionsAgainstHostTests(SimpleTestCase):
    """A pull request cannot pass its composition while an extension fails its checks against that host."""

    def setUp(self):
        self.steps = steps(jobs(workflow("compose.yml"))["compose"])

    def test_every_listed_extension_runs_this_hosts_checks_before_the_composition_passes(self):
        checks = [step for step in self.steps if "check-plugin.sh" in code(step)]
        self.assertEqual(len(checks), 1)
        (check,) = checks
        run = code(check)
        self.assertEqual(condition(check), "github.event_name == 'pull_request'")
        self.assertNotIn("continue-on-error", check)
        self.assertIn("composition-set.json", run)
        self.assertIn(".extensions[]", run)
        self.assertIn("git archive HEAD", run)
        self.assertIn("/host/scripts/check-plugin.sh --plugin-root", run)
        self.assertIn('"$WITHHELD_DIR/', run)
        self.assertIn("exit 1", run)
        self.assertNotIn("GH_TOKEN", check)
        verify = next(index for index, step in enumerate(self.steps) if "scripts/composed-suite.sh" in step)
        self.assertLess(self.steps.index(check), verify)

    def test_an_extension_is_checked_from_its_coordinated_branch_or_its_default_branch(self):
        (clone,) = [step for step in self.steps if "gh repo clone" in step]
        self.assertEqual(condition(clone), "github.event_name == 'pull_request'")
        run = code(clone)
        self.assertIn("coordinated.txt", run)
        self.assertIn(".extensions[].repository", run)
        check = next(index for index, step in enumerate(self.steps) if "check-plugin.sh" in code(step))
        self.assertLess(self.steps.index(clone), check)


@tag("source")
class DeployTests(SimpleTestCase):
    """Production is reached through one approval, by a runner that holds nothing worth taking."""

    def setUp(self):
        self.jobs = jobs(workflow("deploy.yml"))

    def needs(self, job: str) -> set[str]:
        named = block(self.jobs[job], "needs", 4)
        direct = {need.strip(" -[]") for line in named for need in line.split(",")} - {""}
        return direct.union(*(self.needs(need) for need in direct)) if direct else set()

    def test_every_job_on_the_homelab_waits_for_an_approval(self):
        homelab = [name for name, job in self.jobs.items() if "self-hosted" in job]
        self.assertTrue(homelab)
        for name in homelab:
            with self.subTest(job=name):
                gated = {name, *self.needs(name)}
                self.assertTrue(any(block(self.jobs[job], "environment", 4) == ["production"] for job in gated))
        for path in workflows():
            if path.name != "deploy.yml":
                with self.subTest(workflow=path.name):
                    self.assertNotIn("self-hosted", code(path.read_text()))
                    self.assertNotIn("deploy-image.sh", code(path.read_text()))

    def test_the_runner_holds_no_registry_credential_and_verifies_nothing_root_does_not(self):
        deploying = [job for job in self.jobs.values() if "deploy-image.sh" in job]
        self.assertTrue(deploying)
        for job in deploying:
            for absent in ("cosign", ".docker/config.json", "actions/checkout", "contents: read"):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, code(job))
            self.assertIn("| sudo /usr/local/lib/severino-hq/scripts/deploy-image.sh", job)

    def test_a_candidate_is_never_deployed(self):
        text = code(workflow("deploy.yml"))
        self.assertNotIn("coordinated", text)
        self.assertNotIn("candidate", text)


@tag("source")
class PublicLogTests(SimpleTestCase):
    """What a composed build prints is safe to publish in a public log."""

    def setUp(self):
        self.compose = workflow("compose.yml")
        self.steps = steps(jobs(self.compose)["compose"])

    def test_the_composed_suite_prints_through_the_filter(self):
        self.assertNotIn("manage.py test", code(self.compose))
        self.assertIn("scripts/composed-suite.sh", code(self.compose))

    def test_a_scan_writes_a_file_not_the_package_table(self):
        scans = [step for step in self.steps if "aquasecurity/trivy-action@" in step]
        self.assertTrue(scans)
        for scan in scans:
            self.assertIn("format: json", scan)

    def test_withheld_output_leaves_the_runner_only_sealed(self):
        uploads = [step for step in self.steps if "actions/upload-artifact@" in step]
        self.assertTrue(uploads)
        for upload in uploads:
            with self.subTest(step=upload.splitlines()[0]):
                self.assertRegex(block(upload, "path", 10)[0], r"\.age$")
        self.assertIn("scripts/seal-failure-logs.sh", code(self.compose))
        self.assertTrue((ROOT / "scripts" / "seal-failure-logs.sh").is_file())

    def test_sealed_logs_are_encrypted_to_a_post_quantum_key(self):
        pins = (ROOT / "scripts" / "toolchain.env").read_text()
        self.assertRegex(pins, r"(?m)^FAILURE_LOG_RECIPIENT=age1pq1[0-9a-z]+$")

    def test_wheel_digests_reach_the_build_as_a_file_not_an_argument(self):
        # The build log prints each RUN with its build arguments expanded.
        for path in (ROOT / ".github" / "workflows" / "compose.yml", ROOT / "composition" / "Dockerfile"):
            with self.subTest(path=path.name):
                self.assertNotIn("PLUGIN_WHEEL_DIGESTS", path.read_text())

    def test_extension_build_arguments_never_pass_through_a_step_env(self):
        for step in self.steps:
            with self.subTest(step=step.splitlines()[0]):
                for name in ("DIGESTS", "REFERENCES", "ARGS"):
                    self.assertNotRegex(step, rf"\n\s+\w*{name}: \$\{{\{{")

    def test_every_spelling_of_an_extension_is_masked_in_every_case(self):
        masks = [step for step in self.steps if "::add-mask::" in step]
        self.assertTrue(masks)
        for spelling in ("${spelling}", "${spelling^}", "${spelling^^}"):
            with self.subTest(spelling=spelling):
                self.assertTrue(any(f"::add-mask::{spelling}" in step for step in masks))


@tag("source")
class GateReferenceTests(SimpleTestCase):
    def test_every_task_a_workflow_runs_exists(self):
        tasks = set(tomllib.loads((ROOT / "mise.toml").read_text())["tasks"])
        named = [
            (path.name, task.strip("'\""))
            for path in [*workflows(), *composite_actions()]
            for task in re.findall(r"mise run\s+(?:-\S+\s+)*(\S+)", code(path.read_text()))
            if not task.strip("'\"").startswith("$")
        ]
        self.assertGreater(len(named), 3)
        for name, task in named:
            with self.subTest(workflow=name, task=task):
                self.assertIn(task, tasks)

    # Tasks that hand whatever follows them to manage.py, whose command line no
    # usage spec can declare: mise appends the words as they were typed.
    PASSTHROUGH = frozenset({"manage", "suite:as-image"})

    def test_a_task_that_takes_arguments_says_what_they_are(self):
        """``mise run <task> --help`` prints a task's ``usage``. Once one is
        declared mise stops appending arguments to ``run``, so each name it
        declares must reach the task or its script; and a script that reads its
        own arguments needs a usage, or --help describes nothing."""
        tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
        reads_arguments = re.compile(r'"\$@"|\$\{?[1-9#]|sys\.argv')
        checked = 0
        for name, task in tasks.items():
            run = task.get("run", "")
            run = " ".join(run) if isinstance(run, list) else run
            scripts = [ROOT / path for path in re.findall(r"[\w/.-]+\.(?:sh|py)\b", run) if (ROOT / path).is_file()]
            # A test module named the dotted way (`manage.py test a.b.c.Case`).
            for dotted in re.findall(r"\b[a-z_]+(?:\.\w+)+", run):
                parts = dotted.split(".")
                found = next(
                    (
                        ROOT.joinpath(*parts[:end]).with_suffix(".py")
                        for end in range(len(parts), 0, -1)
                        if ROOT.joinpath(*parts[:end]).with_suffix(".py").is_file()
                    ),
                    None,
                )
                if found and found not in scripts:
                    scripts.append(found)
            declared = re.findall(r'(?:arg|flag)\s+"[<\[]?(?:--)?([\w-]+)', task.get("usage", ""))
            with self.subTest(task=name):
                if declared:
                    checked += 1
                    reached = run + "".join(path.read_text() for path in scripts)
                    dropped = [word for word in declared if f"usage_{word.replace('-', '_')}" not in reached]
                    self.assertEqual(dropped, [], f"{name} declares arguments it never forwards")
                elif name not in self.PASSTHROUGH and re.fullmatch(r"[\w/.-]+", run.strip()):
                    for path in scripts:
                        self.assertFalse(
                            reads_arguments.search(path.read_text()), f"{name} takes arguments; declare usage"
                        )
        self.assertGreater(checked, 3)


def compose_plugins():
    spec = importlib.util.spec_from_file_location("compose_plugins", ROOT / "scripts" / "compose-plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tag("source")
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


@tag("source")
class ScriptInputTests(SimpleTestCase):
    """What a script is handed arrives as data, never as its text."""

    def run_blocks(self):
        for path in (*workflows(), *composite_actions()):
            lines = path.read_text().splitlines()
            index = 0
            while index < len(lines):
                match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[index])
                if not match:
                    index += 1
                    continue
                indent, script = len(match.group(1)), [match.group(2)]
                index += 1
                while index < len(lines) and (
                    not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent
                ):
                    script.append(lines[index])
                    index += 1
                yield path.relative_to(ROOT).as_posix(), "\n".join(script)

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
        compose = workflow("compose.yml")
        collect = compose.index('wheel=$(find "$dir" -name')
        check = compose.index("ships a wheel whose name is not a wheel's")
        use = compose.index('--artifact "$wheel"')
        self.assertLess(collect, check)
        self.assertLess(check, use)


def plugin_identity():
    spec = importlib.util.spec_from_file_location("plugin_identity", ROOT / "scripts" / "plugin-identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tag("source")
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
        self.assertEqual(
            found,
            {
                "distribution": "example-alpha",
                "plugin-id": "example.alpha",
                "plugin-reference": "example_alpha.plugin:plugin",
                "django-app": "example_alpha",
            },
        )

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

    def test_a_manifest_naming_another_repository_is_refused_where_it_is_admitted(self):
        package = self.package(self.manifest(source_repository='"owner/example-alpha"'))
        self.assertEqual(plugin_identity().identity(package, "owner/example-alpha")["plugin-id"], "example.alpha")
        with self.assertRaises(SystemExit) as refused:
            plugin_identity().identity(package, "owner/example-extensions")
        self.assertIn("owner/example-extensions", str(refused.exception))

    def test_a_manifest_away_from_its_package_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            plugin_identity().identity(self.package(self.manifest(), name="example-gamma"))
        self.assertIn("src/example_gamma/plugin.py", str(refused.exception))


@tag("source")
class ExtensionCallerTests(SimpleTestCase):
    """An extension's caller names nothing about the extension."""

    def test_the_reusable_checks_and_admission_take_no_identity(self):
        checks = workflow("plugin-checks.yml")
        admit = (ROOT / ".github" / "actions" / "admit-plugin" / "action.yml").read_text()
        for name in ("plugin-reference:", "django-app:", "plugin-id:", "distribution:"):
            with self.subTest(name=name):
                self.assertNotIn(f"\n  {name}", admit.split("\nruns:")[0])
                self.assertNotIn(f"\n      {name}", checks.split("\npermissions:")[0])
        self.assertIn('check-plugin.sh --plugin-root "$PACKAGE_DIR"', checks)
        self.assertIn("scripts/plugin-identity.py", admit)


def action_steps(text: str) -> list[str]:
    """A composite action's steps, split at each ``- name:`` or ``- uses:``."""
    return ["- " + part for part in re.split(r"(?m)^    - ", text.split("\n  steps:\n", 1)[1])[1:]]


@tag("source")
class OneRepositoryManyExtensionsTests(SimpleTestCase):
    """Several extensions in one repository are admitted by one run and composed one by one."""

    def setUp(self):
        self.admit = (ROOT / ".github" / "actions" / "admit-plugin" / "action.yml").read_text()

    def test_each_package_retains_the_artifact_compose_reads_by_its_id(self):
        uploaded = re.search(r"(?m)^\s+name: (\S+)\$\{\{ steps\.identity\.outputs\.plugin-id \}\}$", self.admit)
        self.assertIsNotNone(uploaded, "the admission artifact is not named by plugin id")
        fetched = (ROOT / "scripts" / "admissions.sh").read_text()
        self.assertIn(f'--name "{uploaded.group(1)}$id"', fetched)
        self.assertIn("scripts/admissions.sh fetch", workflow("compose.yml"))

    def test_a_run_starts_the_composition_and_reports_on_its_commit_only_where_it_dispatches(self):
        reaching = [
            step
            for step in action_steps(self.admit)
            if re.search(r"gh workflow run|check-runs|create-github-app-token", code(step))
        ]
        self.assertGreaterEqual(len(reaching), 4)
        for step in reaching:
            with self.subTest(step=step.splitlines()[0]):
                self.assertIn("\n      if: inputs.dispatch == 'true'\n", step)

    def test_every_input_an_admission_dispatches_is_one_compose_declares(self):
        compose = workflow("compose.yml")
        dispatch = compose[compose.index("\n  workflow_dispatch:\n") : compose.index("\npermissions:")]
        declared = set(re.findall(r"(?m)^      ([a-z][a-z-]*):$", dispatch))
        (start,) = [step for step in action_steps(self.admit) if "gh workflow run compose.yml" in step]
        dispatched = re.findall(r"-f ([a-z][a-z-]*)=", start)
        self.assertIn("admission-run", dispatched)
        for name in dispatched:
            with self.subTest(name=name):
                self.assertIn(name, declared)
