"""What the pipeline promises: pinned actions, least privilege, sealed logs, and releases only from main."""

import json
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from django.test import SimpleTestCase, tag

from tests.mise_tasks import body, mise_tasks

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

    def test_every_pinned_commit_carries_its_version_beside_it(self):
        """The comment is what Renovate rewrites with the commit, and what a reader checks."""
        pinned = [
            (path.name, line.strip())
            for path in [*workflows(), *composite_actions()]
            for line in path.read_text().splitlines()
            if re.search(r"uses:\s*[\w.-]+/\S*@[0-9a-f]{40}", line)
        ]
        self.assertGreater(len(pinned), 10)
        for name, line in pinned:
            with self.subTest(file=name, line=line):
                self.assertRegex(line, r"@[0-9a-f]{40} # v\d+(\.\d+){0,2}$")


def toolchain() -> dict[str, str]:
    """``tools/pipeline/toolchain.env``, read as the shell reads it."""
    text = (ROOT / "tools" / "pipeline" / "toolchain.env").read_text()
    return {key: value.strip('"') for key, value in re.findall(r"(?m)^([A-Z_]+)=(.*)$", text)}


def all_jobs() -> list[tuple[str, str, str]]:
    """Every job of every workflow, as (workflow, job id, job text)."""
    return [(path.name, name, job) for path in workflows() for name, job in jobs(path.read_text()).items()]


@tag("source")
class JobLimitTests(SimpleTestCase):
    def test_every_hosted_job_runs_on_the_declared_runner_image(self):
        """``ubuntu-latest`` moves to a new OS on GitHub's schedule."""
        image = toolchain()["RUNNER_IMAGE"]
        hosted = 0
        for path, name, job in all_jobs():
            runner = block(job, "runs-on", 4)
            if not runner or "self-hosted" in " ".join(runner):
                continue
            hosted += 1
            with self.subTest(workflow=path, job=name):
                self.assertEqual(runner, [image])
        self.assertGreater(hosted, 3)

    def test_every_job_has_a_timeout(self):
        """Without one a hung job holds its runner for GitHub's six-hour default. A job
        that calls a reusable workflow cannot take one; the workflow it calls is checked."""
        timed = 0
        for path, name, job in all_jobs():
            if block(job, "uses", 4):
                continue
            timed += 1
            with self.subTest(workflow=path, job=name):
                self.assertTrue(block(job, "timeout-minutes", 4))
        self.assertGreater(timed, 3)


@tag("source")
class PythonVersionTests(SimpleTestCase):
    """What cannot read ``PYTHON_VERSIONS`` is held to it. Its first, oldest, version is
    the one the images run and the floor of requires-python, where ruff and mypy take
    their target."""

    def setUp(self):
        self.versions = toolchain()["PYTHON_VERSIONS"].split()
        self.oldest = self.versions[0]
        self.readme = (ROOT / "README.md").read_text()

    def test_the_readme_badge_names_every_version(self):
        badge = re.search(r"badge/python-(.*?)-blue", self.readme)
        self.assertIsNotNone(badge)
        self.assertEqual(badge.group(1).split("%20%7C%20"), self.versions)

    def test_every_workflow_matrix_runs_every_version(self):
        matrix = block(jobs(workflow("ci.yml"))["tests"], "python", 8)
        self.assertEqual(json.loads(matrix[0]), self.versions)
        (default,) = [
            line for line in block(workflow("plugin-checks.yml"), "python-versions", 6) if line.startswith("default:")
        ]
        self.assertEqual(json.loads(default.removeprefix("default:").strip().strip("'")), self.versions)

    def test_the_images_and_requires_python_take_the_oldest(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertRegex(project["requires-python"], rf"^>={re.escape(self.oldest)}(,|$)")
        named = [
            *(
                re.findall(r"(?m)^FROM python:(\d+\.\d+)-", (ROOT / path).read_text())
                for path in ("Dockerfile", "composition/Dockerfile")
            ),
            re.findall(r"on Python (\d+\.\d+)", self.readme),
        ]
        for found in named:
            self.assertTrue(found)
            self.assertEqual(set(found), {self.oldest})


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

    def test_every_custom_manager_finds_its_pin(self):
        """A file that moves without its pattern, or a line reworded under its
        match string, leaves the pin unwatched and Renovate says nothing."""
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        managers = json.loads((ROOT / ".github" / "renovate.json").read_text())["customManagers"]
        self.assertTrue(managers)
        for manager in managers:
            with self.subTest(manager=manager["description"]):
                patterns = manager.get("managerFilePatterns", manager.get("fileMatch", []))
                self.assertTrue(patterns)
                named: set[str] = set()
                for pattern in patterns:
                    files = {name for name in tracked if name and renovate_file_pattern_matches(pattern, name)}
                    self.assertTrue(files, f"{pattern} matches no tracked file")
                    named |= files
                for match_string in manager["matchStrings"]:
                    expression = re.compile(match_string.replace("(?<", "(?P<"))
                    self.assertTrue(
                        any(expression.search((ROOT / name).read_text()) for name in named),
                        f"{match_string} matches nothing in {sorted(named)}",
                    )


def renovate_file_pattern_matches(pattern: str, name: str) -> bool:
    """Renovate reads ``/…/`` as a regular expression and anything else as a glob."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], name) is not None
    return PurePosixPath(name).full_match(pattern)


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
        """tests/shell/test-extension-branches.sh holds the behavior; this holds the workflow to it."""
        branches = {
            match.group(1): step
            for step in steps(jobs(self.compose)["compose"])
            if (match := re.search(r"tools/pipeline/extension-branches\.sh (\w+)", code(step)))
        }
        self.assertEqual(set(branches), {"checkout", "build"})
        script = code((ROOT / "tools" / "pipeline" / "extension-branches.sh").read_text())
        self.assertIn('rm -rf "$dir/.git"', script)
        for absent in ("GH_TOKEN", "docker.sock", "pip wheel"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, script)
                self.assertNotIn(absent, branches["build"])


@tag("source")
class ExtensionsAgainstHostTests(SimpleTestCase):
    """A pull request cannot pass its composition while an extension fails its checks against that host."""

    def setUp(self):
        self.steps = steps(jobs(workflow("compose.yml"))["compose"])

    def index(self, command: str) -> int:
        (found,) = [index for index, step in enumerate(self.steps) if command in code(step)]
        return found

    def test_every_listed_extension_runs_this_hosts_checks_before_the_composition_passes(self):
        """tests/shell/test-check-extensions.sh holds what the check runs; this holds where it runs."""
        check = self.steps[self.index("tools/pipeline/check-extensions.sh composition-set.json")]
        self.assertEqual(condition(check), "github.event_name == 'pull_request'")
        self.assertNotIn("continue-on-error", check)
        self.assertNotIn("GH_TOKEN", check)
        self.assertLess(self.steps.index(check), self.index("tools/pipeline/composed-suite.sh"))

    def test_an_extension_is_checked_from_its_coordinated_branch_or_its_default_branch(self):
        clone = self.steps[self.index("tools/pipeline/extension-branches.sh checkout")]
        self.assertEqual(condition(clone), "github.event_name == 'pull_request'")
        self.assertIn("coordinated.txt", code(clone))
        self.assertLess(self.index("tools/pipeline/coordinated-branches.sh"), self.steps.index(clone))
        self.assertLess(self.steps.index(clone), self.index("tools/pipeline/check-extensions.sh"))


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
        self.assertIn("tools/pipeline/composed-suite.sh", code(self.compose))

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
        self.assertIn("tools/pipeline/seal-failure-logs.sh", code(self.compose))
        self.assertTrue((ROOT / "tools" / "pipeline" / "seal-failure-logs.sh").is_file())

    def test_sealed_logs_are_encrypted_to_a_post_quantum_key(self):
        pins = (ROOT / "tools" / "pipeline" / "toolchain.env").read_text()
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

    def test_the_extensions_are_masked_before_any_step_can_name_one(self):
        """tools/pipeline/tests/test_composition_set.py holds every spelling; this holds when they are masked."""
        (mask,) = [
            index
            for index, step in enumerate(self.steps)
            if "tools.pipeline.composition_set mask composition-set.json" in code(step)
        ]
        speaking = [
            index
            for index, step in enumerate(self.steps)
            if re.search(r"\bgh\b|\buv\b|GH_TOKEN|docker|manage\.py", code(step))
        ]
        self.assertTrue(speaking)
        self.assertLess(mask, min(speaking))


@tag("source")
class GateReferenceTests(SimpleTestCase):
    def test_every_task_a_workflow_runs_exists(self):
        tasks = set(mise_tasks())
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

    def test_every_browser_gate_has_a_leg_and_every_leg_a_gate(self):
        """The Browser job runs one ``browser:*`` gate per leg; a gate with no leg never runs in CI."""
        (legs,) = block(jobs(workflow("ci.yml"))["browser"], "gate", 8)
        named = {f"browser:{leg.strip()}" for leg in legs.strip("[]").split(",")}
        self.assertEqual(named, {name for name in mise_tasks() if name.startswith("browser:")})

    # Tasks that hand whatever follows them to manage.py, whose command line no
    # usage spec can declare: mise appends the words as they were typed.
    PASSTHROUGH = frozenset({"manage"})

    def test_a_task_that_takes_arguments_says_what_they_are(self):
        """``mise run <task> --help`` prints a task's ``usage``. Once one is
        declared mise stops appending arguments to ``run``, so each name it
        declares must reach the task or its script; and a script that reads its
        own arguments needs a usage, or --help describes nothing."""
        shell_reads = r'"\$@"|\$\{?[1-9]|\$\{?#(?=[\s"\')}]|$)'
        reads_arguments = re.compile(rf"{shell_reads}|sys\.argv", re.MULTILINE)
        checked = 0
        for name, task in mise_tasks().items():
            if task["file"]:
                # A file task declares its usage in its own header, which the list does not carry.
                # A shell task's sys.argv is a Python program's it runs, not its own.
                reads = re.compile(shell_reads, re.MULTILINE) if body(task).startswith("#!/bin/sh") else reads_arguments
                with self.subTest(task=name):
                    if reads.search(body(task)):
                        self.assertRegex(body(task), r"(?m)^#USAGE ", "takes arguments; declare usage")
                continue
            run = " ".join(task["run"])
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
            declared = re.findall(r'(?:arg|flag)\s+"[<\[]?(?:--)?([\w-]+)', task["usage"] or "")
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

    def test_a_task_that_runs_the_suite_takes_the_heavy_turn(self):
        """Two suites at once exhaust a small machine's memory; tools/lib/heavy.sh makes them take turns.
        A TOML task takes it as its shell, a file task by sourcing tools/lib/heavy-turn.sh."""
        suites = {name: task for name, task in mise_tasks().items() if "manage.py test" in body(task)}
        self.assertGreater(len(suites), 3)
        for name, task in suites.items():
            with self.subTest(task=name):
                if task["file"]:
                    self.assertRegex(body(task), r"(?m)^\. .*tools/lib/heavy-turn\.sh\"?$")
                else:
                    self.assertTrue((task["shell"] or "").startswith("tools/lib/heavy.sh "))
                self.assertTrue(task["timeout"], "a hung suite would hold the turn forever")


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
        self.assertIn("tools.pipeline.plugin_identity", admit)


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
        fetched = (ROOT / "tools" / "pipeline" / "admissions.sh").read_text()
        self.assertIn(f'--name "{uploaded.group(1)}$id"', fetched)
        self.assertIn("tools/pipeline/admissions.sh fetch", workflow("compose.yml"))

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
