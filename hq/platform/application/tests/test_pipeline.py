"""What the pipeline promises: one pin per action, least privilege, sealed logs, and releases only from main.

checks:workflows runs zizmor, which holds what any workflow should: actions pinned
to a commit, no persisted checkout credentials, no expression expanded into a
script, no dangerous trigger, no App token with every permission. These hold
HQ's own promises, read through a YAML parser.
"""

import json
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from django.test import SimpleTestCase, tag

from tests.mise_tasks import body, mise_tasks

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github" / "workflows"

type Node = dict[str, Any]


# How a shell script reads its own arguments, and how a Python program does.
SHELL_READS = r'"\$@"|\$\{?[1-9]|\$\{?#(?=[\s"\')}]|$)'
READS_ARGUMENTS = re.compile(rf"{SHELL_READS}|sys\.argv", re.MULTILINE)


def reads_own_arguments(source: str) -> bool:
    """Whether a file task reads the arguments it was given.

    A shell task's sys.argv is a Python program's it runs, not its own. A bare
    `set --` empties the arguments, so after it "$@" is a list the script
    built, the way AGENTS.md passes a list."""

    if not source.startswith("#!/bin/sh"):
        return bool(READS_ARGUMENTS.search(source))
    own = re.split(r"(?m)^\s*set --\s*$", source, maxsplit=1)[0]
    return bool(re.search(SHELL_READS, own, re.MULTILINE))


def forwards(source: str, name: str) -> bool:
    """Whether ``source`` reads the task argument ``name``: as the variable mise
    sets, or through the dev tools' reader of them (hq/platform/core/tests/task.py)."""

    return f"usage_{name}" in source or bool(
        re.search(rf"\.(?:text|switch|repeated|given)\(\s*[\"']{name}[\"']", source)
    )


def workflows() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def composite_actions() -> list[Path]:
    return sorted((ROOT / ".github" / "actions").glob("*/action.yml"))


def load(path: Path) -> Node:
    return yaml.safe_load(path.read_text())


def workflow(name: str) -> Node:
    return load(WORKFLOWS / name)


def triggers(document: Node) -> Node:
    """A workflow's ``on:``, which YAML 1.1, PyYAML's dialect, reads as the key true."""
    return document[True]


def jobs(document: Node) -> dict[str, Node]:
    return document["jobs"]


def steps(node: Node) -> list[Node]:
    """A job's steps, or a composite action's; a job that calls a workflow has none."""
    return node["runs"]["steps"] if "runs" in node else node.get("steps", [])


def every_step(path: Path) -> list[Node]:
    """Every step of a workflow or a composite action."""
    document = load(path)
    return steps(document) if "runs" in document else [step for job in jobs(document).values() for step in steps(job)]


def all_jobs() -> list[tuple[str, str, Node]]:
    """Every job of every workflow, as (workflow, job id, job)."""
    return [(path.name, name, job) for path in workflows() for name, job in jobs(load(path)).items()]


def uses(node: Any) -> list[str]:
    """Every ``uses:`` beneath a node: a step's action, a job's reusable workflow."""
    if isinstance(node, list):
        return [found for each in node for found in uses(each)]
    if not isinstance(node, dict):
        return []
    own = [node["uses"]] if isinstance(node.get("uses"), str) else []
    return own + [found for key, value in node.items() if key != "uses" for found in uses(value)]


def needed(job: Node) -> set[str]:
    """The job ids a job's ``needs:`` names."""
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


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


def condition(node: Node) -> str:
    """A job's or step's ``if:``; empty when it has none."""
    return str(node.get("if", ""))


def code(text: str) -> str:
    """The text without its comment lines."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def script(step: Node) -> str:
    """A step's ``run:`` script, without its comment lines."""
    return code(step.get("run", ""))


def text(node: Any) -> str:
    """Every key and string beneath a node, one per line: what a step names anywhere in it."""
    if isinstance(node, dict):
        return "\n".join(f"{key}\n{text(value)}" for key, value in node.items())
    if isinstance(node, list):
        return "\n".join(text(each) for each in node)
    return code(node) if isinstance(node, str) else ""


def label(step: Node) -> str:
    return step.get("name") or step.get("uses") or (step.get("run", "").splitlines() or ["a step"])[0]


@tag("source")
class ActionPinTests(SimpleTestCase):
    def test_every_docker_action_is_pinned_by_digest(self):
        """zizmor's unpinned-uses passes over a ``docker://`` action, tagged or not."""
        for path in [*workflows(), *composite_actions()]:
            for action in uses(load(path)):
                if action.startswith("docker://"):
                    with self.subTest(file=path.name, action=action):
                        self.assertRegex(action, r"@sha256:[0-9a-f]{64}$")

    def test_every_action_has_one_version_across_workflows_and_composite_actions(self):
        pins: dict[str, set[str]] = {}
        for path in [*workflows(), *composite_actions()]:
            for action in uses(load(path)):
                if match := re.fullmatch(r"([\w./-]+)@([0-9a-f]{40})", action):
                    pins.setdefault(match.group(1), set()).add(match.group(2))
        self.assertGreater(len(pins), 5)
        self.assertEqual({action: shas for action, shas in pins.items() if len(shas) > 1}, {})

    def test_every_pinned_commit_carries_its_version_beside_it(self):
        """The comment is what Renovate rewrites with the commit, and what a reader
        checks. A comment is no part of the parsed document, so this reads lines."""
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


def saves_a_cache(step: Node) -> bool:
    return step.get("uses", "").startswith("actions/cache/save@")


@tag("source")
class CacheWriterTests(SimpleTestCase):
    """A branch reads the default branch's caches and writes none, so a pull request adds no entries."""

    def test_no_cache_is_saved_by_the_action_that_saves_on_every_run(self):
        for path in [*workflows(), *composite_actions()]:
            with self.subTest(file=path.name):
                self.assertNotIn("actions/cache", [action.split("@")[0] for action in uses(load(path))])

    def test_a_cache_is_saved_only_from_the_default_branch(self):
        """A job that runs only on a push is on main: ci.yml pushes nowhere else."""
        saves = [(path, job, step) for path, _, job in all_jobs() for step in steps(job) if saves_a_cache(step)]
        self.assertGreater(len(saves), 1)
        for name, job, step in saves:
            with self.subTest(file=name, step=label(step)):
                where = f"{condition(job)} {condition(step)}"
                self.assertRegex(where, r"github\.event\.repository\.default_branch|github\.event_name == 'push'")

    def test_an_action_saves_a_cache_only_for_a_job_on_the_default_branch(self):
        """A composite action saves on the default branch, or when its caller
        passes ``save: true``, which only a job that runs on a push does."""
        saves = [
            (path.parent.name, step) for path in composite_actions() for step in every_step(path) if saves_a_cache(step)
        ]
        self.assertTrue(saves)
        for action, step in saves:
            with self.subTest(action=action):
                self.assertRegex(condition(step), r"github\.event\.repository\.default_branch|inputs\.save == 'true'")
        callers = [
            (path, job)
            for path, _, job in all_jobs()
            for step in steps(job)
            if step.get("with", {}).get("save") in (True, "true")
        ]
        self.assertTrue(callers)
        for name, job in callers:
            with self.subTest(file=name):
                self.assertRegex(condition(job), r"github\.event_name == 'push'")


BROWSER_SETUP = "setup:browser"


def step_tasks(step: Node) -> list[re.Pattern[str]]:
    """The tasks a step runs, each as a pattern: a ``$NAME`` is read from the
    step's ``env:``, where a ``${{ }}`` expression stands for any text."""
    found = []
    for task in re.findall(r"mise run\s+(?:-\S+\s+)*(\S+)", script(step)):
        name = task.strip("'\"")
        if name.startswith("$"):
            name = str(step.get("env", {}).get(name.strip("${}"), name))
        parts = re.split(r"\$\{\{.*?\}\}", name)
        found.append(re.compile(".*".join(re.escape(part) for part in parts)))
    return found


def needs_a_browser(task: dict[str, Any]) -> bool:
    # A dependency given with arguments is listed as [name, *arguments]; a task
    # run as a step, as {"task": name, ...}.
    depends = {each if isinstance(each, str) else each[0] for each in task["depends"]}
    steps = {each["task"] for each in task["run"] if isinstance(each, dict) and "task" in each}
    return BROWSER_SETUP in depends | steps


def locked_version(package: str) -> str:
    """The version uv.lock pins for ``package``."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    (version,) = [entry["version"] for entry in lock["package"] if entry["name"] == package]
    return version


def container_image(job: Node) -> str:
    container = job.get("container", "")
    return container if isinstance(container, str) else container.get("image", "")


@tag("source")
class BrowserInstallTests(SimpleTestCase):
    def test_every_job_that_runs_a_browser_gate_runs_in_the_playwright_image_the_lock_pins(self):
        """Playwright's image carries the browsers of one Playwright release and their
        system libraries, so a job installs nothing from apt or a browser download only
        while that release is the one uv.lock pins. A bump of one without the other
        fails here rather than downloading at run time. Pinned by digest, which
        Renovate moves with the tag."""
        tasks = mise_tasks()
        gates = {name for name, task in tasks.items() if name != BROWSER_SETUP and needs_a_browser(task)}
        image = f"mcr.microsoft.com/playwright/python:v{locked_version('playwright')}-noble"
        checked = 0
        for path, name, job in all_jobs():
            ran = {task for step in steps(job) for task in tasks if any(p.fullmatch(task) for p in step_tasks(step))}
            if not gates & ran:
                continue
            checked += 1
            with self.subTest(workflow=path, job=name):
                self.assertRegex(container_image(job), rf"^{re.escape(image)}@sha256:[0-9a-f]{{64}}$")
        self.assertGreater(checked, 1)


def toolchain() -> dict[str, str]:
    """``tools/pipeline/toolchain.env``, read as the shell reads it."""
    text = (ROOT / "tools" / "pipeline" / "toolchain.env").read_text()
    return {key: value.strip('"') for key, value in re.findall(r"(?m)^([A-Z_]+)=(.*)$", text)}


@tag("source")
class JobLimitTests(SimpleTestCase):
    def test_every_hosted_job_runs_on_one_versioned_runner_image(self):
        """``ubuntu-latest`` moves to a new OS on GitHub's schedule; Renovate moves a
        versioned label in every workflow at once."""
        runners = [job["runs-on"] for _, _, job in all_jobs() if "runs-on" in job]
        hosted = {runner for runner in runners if "self-hosted" not in runner}
        self.assertEqual(len(hosted), 1, hosted)
        self.assertNotIn("latest", hosted.pop())

    def test_every_job_has_a_timeout(self):
        """Without one a hung job holds its runner for GitHub's six-hour default. A job
        that calls a reusable workflow cannot take one; the workflow it calls is checked."""
        timed = 0
        for path, name, job in all_jobs():
            if "uses" in job:
                continue
            timed += 1
            with self.subTest(workflow=path, job=name):
                self.assertIn("timeout-minutes", job)
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
        self.assertEqual(jobs(workflow("ci.yml"))["tests"]["strategy"]["matrix"]["python"], self.versions)
        reusable = triggers(workflow("plugin-checks.yml"))["workflow_call"]["inputs"]["python-versions"]
        self.assertEqual(json.loads(reusable["default"]), self.versions)

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
            found = jobs(load(path))
            needs = {name: needed(job) for name, job in found.items()}
            skippable = {name for name, job in found.items() if condition(job)}
            for name, job in found.items():
                expression = condition(job)
                if not expression or re.search(r"\b(always|cancelled|success|failure)\(\)", expression):
                    continue
                checked += 1
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual(upstream(name, needs) & skippable, set())
        self.assertGreater(checked, 1)


@tag("source")
class LeastPrivilegeTests(SimpleTestCase):
    def test_no_workflow_writes_a_check_of_its_own(self):
        # A job's result is its check, and the deployment says where a commit
        # stands: the ruleset requires Ready, from GitHub Actions.
        for path in workflows():
            with self.subTest(workflow=path.name):
                self.assertIsNone(re.search(r"check-runs|permission-checks|checks: write", code(path.read_text())))

    def test_only_a_job_that_signs_or_publishes_holds_an_identity_token(self):
        # The actions whose only work is to present the job's OIDC identity.
        presenters = ("actions/deploy-pages@", "ossf/scorecard-action@", "./.github/actions/cosign")
        holders = 0
        for path, name, job in all_jobs():
            if job.get("permissions", {}).get("id-token") != "write":
                continue
            holders += 1
            called = [(ROOT / action).read_text() for action in uses(job) if action.startswith("./.github/workflows/")]
            with self.subTest(workflow=path, job=name):
                self.assertTrue(
                    any("cosign sign" in script(step) for step in steps(job))
                    or any(action.startswith(presenters) for action in uses(job))
                    or any("cosign sign" in source for source in called)
                )
        self.assertGreater(holders, 0)


LEVELS = {"read": 1, "write": 2, "admin": 3}


def app_token_mints(path: Path) -> list[tuple[str, dict[str, str]]]:
    """Each ``create-github-app-token`` step in a workflow or composite action, with the permissions it asks for."""
    return [
        (
            f"{path.parent.name if path.name == 'action.yml' else path.name}: {label(step)}",
            {
                name.removeprefix("permission-").replace("-", "_"): str(level)
                for name, level in step.get("with", {}).items()
                if name.startswith("permission-")
            },
        )
        for step in every_step(path)
        if step.get("uses", "").startswith("actions/create-github-app-token@")
    ]


def beyond_declaration(requested: dict[str, str], declared: dict[str, str]) -> list[str]:
    """What a mint asks for past the App's declaration. zizmor's github-app
    audit refuses a mint that asks for nothing named, and so everything."""
    return [
        f"{name}: {level}"
        for name, level in requested.items()
        if LEVELS.get(level, len(LEVELS) + 1) > LEVELS.get(declared.get(name, ""), 0)
    ]


@tag("source")
class AppTokenScopeTests(SimpleTestCase):
    """The App's declaration is the one record of what it can do, so no workflow asks it for more."""

    def test_every_app_token_asks_only_for_what_the_app_declares(self):
        declared = json.loads((ROOT / "deploy" / "github-apps.json").read_text())["apps"]["hq"]["permissions"]
        mints = [mint for path in [*workflows(), *composite_actions()] for mint in app_token_mints(path)]
        self.assertGreater(len(mints), 5)
        for step, requested in mints:
            with self.subTest(step=step):
                self.assertEqual(beyond_declaration(requested, declared), [])


@tag("source", "tree")
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
        (allowed,) = [
            json.loads(step["env"]["RENOVATE_ALLOWED_COMMANDS"])
            for step in every_step(WORKFLOWS / "renovate.yml")
            if "RENOVATE_ALLOWED_COMMANDS" in step.get("env", {})
        ]
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
        self.steps = steps(jobs(self.compose)["compose"])

    def test_a_release_is_a_dispatch_on_main_and_nothing_else(self):
        defined = re.fullmatch(r"\$\{\{(.*)\}\}", self.compose["env"]["IS_RELEASE"])
        self.assertIsNotNone(defined)
        terms = {term.strip() for term in defined.group(1).split("&&")}
        self.assertEqual(terms, {"github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/main'"})

    def test_the_composition_is_pushed_and_signed_only_in_a_release(self):
        publishing = [step for step in self.steps if re.search(r"docker push|cosign sign\b", script(step))]
        self.assertGreaterEqual(len(publishing), 2)
        for step in publishing:
            with self.subTest(step=label(step)):
                gate = condition(step)
                self.assertTrue(gate == "env.IS_RELEASE == 'true'" or gate.startswith("env.IS_RELEASE == 'true' &&"))
                self.assertNotIn("candidate", text(step))

    def test_branch_code_builds_without_the_jobs_credentials(self):
        """tests/shell/test-extension-branches.sh holds the behavior; this holds the workflow to it."""
        branches = {
            match.group(1): text(step)
            for step in self.steps
            if (match := re.search(r"tools/pipeline/extension-branches\.sh (\w+)", script(step)))
        }
        self.assertEqual(set(branches), {"checkout", "build"})
        source = code((ROOT / "tools" / "pipeline" / "extension-branches.sh").read_text())
        for absent in ("GH_TOKEN", "docker.sock", "pip wheel"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, source)
                self.assertNotIn(absent, branches["build"])


# The jobs in ci.yml that gate nothing: the cache writer and the two verdicts.
NOT_GATES = {"caches", "ready", "release"}
# Gates Ready waits for and Release does not: code scanning holds a merge, not a release.
MERGE_ONLY = {"codeql"}


def pull_request_only(job: Node) -> bool:
    """Whether a job's ``if:`` requires a pull request event, so a push skips it."""
    expression = condition(job)
    return "github.event_name == 'pull_request'" in expression and "||" not in expression


@tag("source")
class VerdictNeedsTests(SimpleTestCase):
    """A gate added to CI is one the verdicts wait for, so neither reports a pass the gate did not give."""

    def setUp(self):
        self.jobs = jobs(workflow("ci.yml"))

    def test_ready_waits_for_every_gate(self):
        self.assertEqual(needed(self.jobs["ready"]), set(self.jobs) - NOT_GATES)

    def test_release_waits_for_every_gate_a_push_runs(self):
        skipped_on_push = {name for name, job in self.jobs.items() if pull_request_only(job)}
        self.assertTrue(pull_request_only(self.jobs["ready"]))
        self.assertFalse(pull_request_only(self.jobs["release"]))
        self.assertEqual(needed(self.jobs["release"]), set(self.jobs) - NOT_GATES - MERGE_ONLY - skipped_on_push)


@tag("source")
class ExtensionsAgainstHostTests(SimpleTestCase):
    """A pull request cannot pass its composition while an extension fails its checks against that host."""

    def setUp(self):
        self.steps = steps(jobs(workflow("compose.yml"))["compose"])

    def index(self, command: str) -> int:
        (found,) = [index for index, step in enumerate(self.steps) if command in script(step)]
        return found

    def test_every_listed_extension_runs_this_hosts_checks_before_the_composition_passes(self):
        """tests/shell/test-check-extensions.sh holds what the check runs; this holds where it runs."""
        check = self.index("tools/pipeline/check-extensions.sh composition-set.json")
        self.assertEqual(condition(self.steps[check]), "github.event_name == 'pull_request'")
        self.assertNotIn("continue-on-error", self.steps[check])
        self.assertNotIn("GH_TOKEN", text(self.steps[check]))
        self.assertLess(check, self.index("tools/pipeline/composed-suite.sh"))

    def test_an_extension_is_checked_from_its_coordinated_branch_or_its_default_branch(self):
        clone = self.index("tools/pipeline/extension-branches.sh checkout")
        self.assertEqual(condition(self.steps[clone]), "github.event_name == 'pull_request'")
        self.assertIn("coordinated.txt", script(self.steps[clone]))
        self.assertLess(self.index("tools/pipeline/coordinated-branches.sh"), clone)
        self.assertLess(clone, self.index("tools/pipeline/check-extensions.sh"))


@tag("source")
class DeployTests(SimpleTestCase):
    """Production is reached through one approval, by a runner that holds nothing worth taking."""

    def setUp(self):
        self.jobs = jobs(workflow("deploy.yml"))

    def needs(self, job: str) -> set[str]:
        return upstream(job, {name: needed(each) for name, each in self.jobs.items()})

    def environment(self, job: str) -> Node:
        return self.jobs[job].get("environment", {})

    def test_every_job_on_the_homelab_waits_for_an_approval(self):
        homelab = [name for name, job in self.jobs.items() if "self-hosted" in job.get("runs-on", [])]
        self.assertTrue(homelab)
        for name in homelab:
            with self.subTest(job=name):
                gated = {name, *self.needs(name)}
                self.assertTrue(any(self.environment(job).get("name") == "production" for job in gated))
        for path in workflows():
            if path.name != "deploy.yml":
                with self.subTest(workflow=path.name):
                    self.assertNotIn("self-hosted", code(path.read_text()))
                    self.assertNotIn("hq-host deploy", code(path.read_text()))

    def test_the_runner_holds_no_registry_credential_and_verifies_nothing_root_does_not(self):
        deploying = [job for job in self.jobs.values() if "hq-host deploy" in text(job)]
        self.assertTrue(deploying)
        for job in deploying:
            for absent in ("cosign", ".docker/config.json", "actions/checkout"):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, text(job))
            self.assertNotIn("contents", job.get("permissions", {}))
            self.assertTrue(
                any(
                    "| sudo /usr/local/lib/severino-hq/deploy/bin/hq-host deploy " in script(step)
                    for step in steps(job)
                )
            )

    def test_the_deployment_is_recorded_for_the_commit_deployed_and_no_job_records_its_own(self):
        """A job's own deployment names the commit main was at when the run started,
        and succeeds once approved: Resolve records the one for the resolved commit."""
        environments = [name for name in self.jobs if self.environment(name)]
        self.assertTrue(environments)
        for name in environments:
            with self.subTest(job=name):
                self.assertIs(self.environment(name).get("deployment"), False)
        (record,) = [step for step in steps(self.jobs["resolve"]) if '/deployments"' in script(step)]
        self.assertEqual(record["env"]["COMMIT"], "${{ steps.find.outputs.commit }}")
        self.assertIn('--arg ref "$COMMIT"', script(record))
        self.assertIn("ref: $ref", script(record))

    def test_a_candidate_is_never_deployed(self):
        source = code((WORKFLOWS / "deploy.yml").read_text())
        self.assertNotIn("coordinated", source)
        self.assertNotIn("candidate", source)


@tag("source")
class PublicLogTests(SimpleTestCase):
    """What a composed build prints is safe to publish in a public log."""

    def setUp(self):
        self.compose = code((WORKFLOWS / "compose.yml").read_text())
        self.steps = steps(jobs(workflow("compose.yml"))["compose"])

    def test_the_composed_suite_prints_through_the_filter(self):
        self.assertNotIn("manage.py test", self.compose)
        self.assertIn("tools/pipeline/composed-suite.sh", self.compose)

    def test_a_scan_writes_a_file_not_the_package_table(self):
        scans = [step for step in self.steps if step.get("uses", "").startswith("aquasecurity/trivy-action@")]
        self.assertTrue(scans)
        for scan in scans:
            self.assertEqual(scan["with"]["format"], "json")
            # The Trivy mise.toml pins, never one the action downloads.
            self.assertIs(scan["with"]["skip-setup-trivy"], True)

    def test_withheld_output_leaves_the_runner_only_sealed(self):
        uploads = [step for step in self.steps if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertTrue(uploads)
        for upload in uploads:
            with self.subTest(step=label(upload)):
                self.assertRegex(upload["with"]["path"], r"\.age$")
        self.assertIn("tools/pipeline/seal-failure-logs.sh", self.compose)
        self.assertTrue((ROOT / "tools" / "pipeline" / "seal-failure-logs.sh").is_file())

    def test_sealed_logs_are_encrypted_to_a_post_quantum_key(self):
        pins = (ROOT / "tools" / "pipeline" / "toolchain.env").read_text()
        self.assertRegex(pins, r"(?m)^FAILURE_LOG_RECIPIENT=age1pq1[0-9a-z]+$")

    def test_wheel_digests_reach_the_build_as_a_file_not_an_argument(self):
        # The build log prints each RUN with its build arguments expanded.
        for path in (WORKFLOWS / "compose.yml", ROOT / "composition" / "Dockerfile"):
            with self.subTest(path=path.name):
                self.assertNotIn("PLUGIN_WHEEL_DIGESTS", path.read_text())

    def test_extension_build_arguments_never_pass_through_a_step_env(self):
        for step in self.steps:
            with self.subTest(step=label(step)):
                for name, value in step.get("env", {}).items():
                    if any(part in name for part in ("DIGESTS", "REFERENCES", "ARGS")):
                        self.assertNotRegex(str(value), r"^\$\{\{")

    def test_the_extensions_are_masked_before_any_step_can_name_one(self):
        """tools/pipeline/tests/test_composition_set.py holds every spelling; this holds when they are masked."""
        (mask,) = [
            index
            for index, step in enumerate(self.steps)
            if "tools.pipeline.composition_set mask composition-set.json" in script(step)
        ]
        speaking = [
            index
            for index, step in enumerate(self.steps)
            if re.search(r"\bgh\b|\buv\b|GH_TOKEN|docker|manage\.py", text(step))
        ]
        self.assertTrue(speaking)
        self.assertLess(mask, min(speaking))


@tag("source")
class WorkflowSecrecyTests(SimpleTestCase):
    """A private value reaches a public log through the environment or not at all.

    Actions expands ``${{ … }}`` into the text of the step it then echoes, so a
    value interpolated into a script body is printed in full before the step
    runs. Passed through ``env:`` it is a shell variable the echo never sees,
    and a secret is masked on top of that. zizmor's template-injection holds
    every other context; a secret it leaves alone.
    """

    def test_no_script_body_interpolates_a_secret(self):
        offenders = [
            f"{path.name}: {label(step)}"
            for path in [*workflows(), *composite_actions()]
            for step in every_step(path)
            if re.search(r"\$\{\{\s*secrets\.", step.get("run", ""))
        ]
        self.assertEqual(offenders, [], "pass it through env, instead: a script body is echoed verbatim")

    def test_the_composition_set_is_read_from_a_secret(self):
        """A variable is not masked, and this inventory is the private half."""
        # Asserted on names rather than on contents: a failure that printed the
        # file would put the whole workflow in the output of the check meant to
        # keep things out of it.
        offenders = [path.name for path in workflows() if "vars.COMPOSITION_EXTENSIONS" in path.read_text()]
        self.assertEqual(offenders, [], "read it from secrets, which are masked")


@tag("source")
class GateReferenceTests(SimpleTestCase):
    def test_every_task_a_workflow_runs_exists(self):
        tasks = set(mise_tasks())
        named = [
            (path.name, task.strip("'\""))
            for path in [*workflows(), *composite_actions()]
            for step in every_step(path)
            for task in re.findall(r"mise run\s+(?:-\S+\s+)*(\S+)", script(step))
            if not task.strip("'\"").startswith("$")
        ]
        self.assertGreater(len(named), 3)
        for name, task in named:
            with self.subTest(workflow=name, task=task):
                self.assertIn(task, tasks)

    def test_every_browser_gate_has_a_leg_and_every_leg_a_gate(self):
        """The Browser job runs one ``browser:*`` gate per leg; a gate with no leg never runs in CI."""
        legs = jobs(workflow("ci.yml"))["browser"]["strategy"]["matrix"]["gate"]
        self.assertEqual(
            {f"browser:{leg}" for leg in legs}, {name for name in mise_tasks() if name.startswith("browser:")}
        )

    # Tasks that hand whatever follows them to manage.py, whose command line no
    # usage spec can declare: mise appends the words as they were typed.
    PASSTHROUGH = frozenset({"manage"})

    def test_a_task_that_takes_arguments_says_what_they_are(self):
        """``mise run <task> --help`` prints a task's ``usage``. Once one is
        declared mise stops appending arguments to ``run``, so each name it
        declares must reach the task or its script; and a script that reads its
        own arguments needs a usage, or --help describes nothing."""
        checked = 0
        for name, task in mise_tasks().items():
            if task["file"]:
                # A file task declares its usage in its own header, which the list does not carry.
                with self.subTest(task=name):
                    if reads_own_arguments(body(task)):
                        self.assertRegex(body(task), r"(?m)^#USAGE ", "takes arguments; declare usage")
                continue
            run = body(task)
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
                    dropped = [word for word in declared if not forwards(reached, word.replace("-", "_"))]
                    self.assertEqual(dropped, [], f"{name} declares arguments it never forwards")
                    # mise parsed them already: a second parser is a second declaration that drifts from the usage.
                    parsing = [
                        path.name
                        for path in scripts
                        if re.search(r"(?m)^\s*(?:import|from) argparse\b", path.read_text())
                    ]
                    self.assertEqual(parsing, [], f"{name} parses its own flags; read mise's parse through task.Usage")
                elif name not in self.PASSTHROUGH and re.fullmatch(r"[\w/.-]+", run.strip()):
                    for path in scripts:
                        self.assertFalse(
                            READS_ARGUMENTS.search(path.read_text()), f"{name} takes arguments; declare usage"
                        )
        self.assertGreater(checked, 3)

    def test_a_script_reads_its_arguments_unless_it_built_the_list_itself(self):
        reads = '#!/bin/sh\nexec tool "$@"\n'
        built = '#!/bin/sh\nset --\nfor word in a b; do\n    set -- "$@" "$word"\ndone\nexec tool "$@"\n'
        read_before_clearing = '#!/bin/sh\necho "$1"\nset --\nexec tool "$@"\n'
        self.assertTrue(reads_own_arguments(reads))
        self.assertFalse(reads_own_arguments(built))
        self.assertTrue(reads_own_arguments(read_before_clearing))

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

    def test_a_task_declared_outside_mise_toml_names_no_file_in_its_env(self):
        """mise reads an env file, and renders ``config_root`` inside env, from the
        directory of the file that declares the task: in tasks/ or mise-tasks/ a
        checkout path would point beneath that directory instead."""
        declared_elsewhere = {
            name: task for name, task in mise_tasks().items() if Path(task["config_sources"][0]) != ROOT / "mise.toml"
        }
        self.assertGreater(len(declared_elsewhere), 10)
        for name, task in declared_elsewhere.items():
            paths = [entry for entry in task["env"] if entry.startswith("_.") or "config_root" in entry]
            with self.subTest(task=name):
                self.assertEqual(paths, [])


ADMIT = ROOT / ".github" / "actions" / "admit-plugin" / "action.yml"


@tag("source")
class ExtensionCallerTests(SimpleTestCase):
    """An extension's caller names nothing about the extension."""

    def test_the_reusable_checks_and_admission_take_no_identity(self):
        checks = workflow("plugin-checks.yml")
        admit = load(ADMIT)
        for name in ("plugin-reference", "django-app", "plugin-id", "distribution"):
            with self.subTest(name=name):
                self.assertNotIn(name, admit["inputs"])
                self.assertNotIn(name, triggers(checks)["workflow_call"]["inputs"])
        self.assertIn('tools/pipeline/check-plugin.sh --plugin-root "$PACKAGE_DIR"', text(checks))
        self.assertIn("tools.pipeline.plugin_identity", text(admit))

    def test_an_admission_audits_its_lock_as_the_host_audits_its_own(self):
        (audit,) = [step for step in every_step(ADMIT) if "Audit locked dependencies" in step.get("name", "")]
        self.assertIn('host/tools/pipeline/audit.sh "$PACKAGE_DIR"', script(audit))
        self.assertIn("tools/pipeline/audit.sh", body(mise_tasks()["checks:audit"]))


@tag("source")
class OneRepositoryManyExtensionsTests(SimpleTestCase):
    """Several extensions in one repository are admitted by one run and composed one by one."""

    def setUp(self):
        self.steps = every_step(ADMIT)

    def test_each_package_retains_the_artifact_compose_reads_by_its_id(self):
        (uploaded,) = [
            match
            for step in self.steps
            if (
                match := re.fullmatch(
                    r"(\S+)\$\{\{ steps\.identity\.outputs\.plugin-id \}\}", str(step.get("with", {}).get("name"))
                )
            )
        ]
        fetched = (ROOT / "tools" / "pipeline" / "admissions.sh").read_text()
        self.assertIn(f'--name "{uploaded.group(1)}$id"', fetched)
        self.assertIn("tools/pipeline/admissions.sh fetch", text(workflow("compose.yml")))

    def test_a_run_starts_the_composition_and_reports_on_its_commit_only_where_it_dispatches(self):
        reaching = [
            step
            for step in self.steps
            if re.search(r"gh workflow run|check-runs", script(step))
            or step.get("uses", "").startswith("actions/create-github-app-token@")
        ]
        self.assertGreaterEqual(len(reaching), 4)
        for step in reaching:
            with self.subTest(step=label(step)):
                self.assertEqual(condition(step), "inputs.dispatch == 'true'")

    def test_every_input_an_admission_dispatches_is_one_compose_declares(self):
        declared = triggers(workflow("compose.yml"))["workflow_dispatch"]["inputs"]
        (start,) = [step for step in self.steps if "gh workflow run compose.yml" in script(step)]
        dispatched = re.findall(r"-f ([a-z][a-z-]*)=", script(start))
        self.assertIn("admission-run", dispatched)
        for name in dispatched:
            with self.subTest(name=name):
                self.assertIn(name, declared)
