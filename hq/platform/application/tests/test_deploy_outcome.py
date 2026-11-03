"""What a Deploy run says happened, from the deploy step's exit status to the deployment's status and summary.

The deploy step and the Report step run as written in deploy.yml, under bash
as GitHub runs them, with `sudo` and `gh` faked on PATH. What each exit status
means is deploy/outcomes.json's, which hq-host's statuses are generated from.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, tag

from .test_pipeline import ROOT, jobs, script, steps, workflow

SERVER = "https://github.example.com"
REPOSITORY = "example/hq"
IMAGE = f"ghcr.example.com/{REPOSITORY}/composition@sha256:{'7c1b' * 16}"
RELEASE = "e" * 40
BEFORE = "c" * 40
NEWER = "f" * 40

FAKE_SUDO = """#!/bin/sh
cat >/dev/null
exit "$SUDO_EXIT"
"""

# `gh api PATH [--input -] [--silent]`: a write is kept, a read answers from a file.
FAKE_GH = """#!/bin/sh
if [ "$3" = --input ]; then
  cat >> "$FAKE/posted"
  exit 0
fi
case "$2" in
  *"/deployments?"*) cat "$FAKE/deployments.json" ;;
  */jobs*) [ "$GH_TOKEN" = example-actions-token ] && [ "$4" = --slurp ] && { printf "["; cat "$FAKE/jobs.json" && printf "]"; } ;;
  */statuses) id="${2%/statuses}"; cat "$FAKE/statuses-${id##*/}.json" ;;
  *) exit 1 ;;
esac
"""


def declared():
    """deploy/outcomes.json's outcomes, by name."""
    return json.loads((ROOT / "deploy" / "outcomes.json").read_text())["outcomes"]


def deployment(ident, sha, created_at):
    """One entry of `GET /repos/{owner}/{repo}/deployments`, as the REST API returns it."""
    return {
        "id": ident,
        "sha": sha,
        "ref": sha,
        "task": "deploy",
        "environment": "production",
        "production_environment": True,
        "created_at": created_at,
        "payload": {"image": IMAGE},
    }


def status(state):
    """One entry of `GET .../deployments/{id}/statuses`."""
    return {"state": state, "environment": "production", "created_at": "2026-10-31T00:00:00Z"}


def step(name, number, conclusion):
    """One step of a job in `GET .../actions/runs/{id}/attempts/{n}/jobs`. A step the runner
    skipped is recorded with times all the same, so only its conclusion says it never ran."""
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "number": number,
        "started_at": "2026-10-31T02:30:00Z",
        "completed_at": "2026-10-31T02:31:00Z",
    }


@tag("source")
class DeployOutcomeTests(SimpleTestCase):
    def setUp(self):
        self.outcomes = declared()
        found = jobs(workflow("deploy.yml"))
        self.deploy_job = found["deploy"]
        (self.deploy,) = [step for step in steps(found["deploy"]) if "hq-host deploy" in script(step)]
        (self.report,) = [step for step in steps(found["report"]) if "/statuses" in script(step)]
        self.fake = Path(self.enterContext(TemporaryDirectory()))
        bin_dir = self.fake / "bin"
        bin_dir.mkdir()
        for name, body in (("sudo", FAKE_SUDO), ("gh", FAKE_GH)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        (bin_dir / "python3").symlink_to(sys.executable)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.history(
            {
                2: (BEFORE, "2026-10-25T00:36:00Z", [status("inactive"), status("success")]),
                5: (RELEASE, "2026-10-31T02:19:12Z", [status("in_progress"), status("queued")]),
            }
        )

    def history(self, deployments):
        """GitHub's production deployments: id to (commit, created_at, statuses, newest first)."""
        listed = [deployment(ident, sha, at) for ident, (sha, at, _) in deployments.items()]
        (self.fake / "deployments.json").write_text(json.dumps(sorted(listed, key=lambda each: each["created_at"])))
        for ident, (_, _, kept) in deployments.items():
            (self.fake / f"statuses-{ident}.json").write_text(json.dumps(kept))

    def run_step(self, step, **env):
        body = self.fake / "step.sh"
        body.write_text(step["run"])
        for name in ("output", "summary", "posted"):
            (self.fake / name).write_text("")
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(body)],
            cwd=ROOT,
            env={
                "PATH": self.path,
                "FAKE": str(self.fake),
                "GITHUB_OUTPUT": str(self.fake / "output"),
                "GITHUB_STEP_SUMMARY": str(self.fake / "summary"),
                "GITHUB_SERVER_URL": SERVER,
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_RUN_ID": "1",
                "GITHUB_RUN_ATTEMPT": "1",
                "IMAGE": IMAGE,
                "REGISTRY_USER": "example",
                "REGISTRY_TOKEN": "example-token",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def deploy_ends(self, exit_status):
        """The deploy step run against an hq-host exiting ``exit_status``: its exit and its outputs."""
        done = self.run_step(self.deploy, SUDO_EXIT=str(exit_status))
        outputs = dict(line.split("=", 1) for line in (self.fake / "output").read_text().splitlines())
        return done.returncode, outputs

    def reported(self, outputs, result, commit=RELEASE):
        """The Report step for a deploy that ended so: the status it posted, and its summary."""
        # Report reads the deploy job's outputs as its env names them, through the job's outputs.
        env = {}
        for name, value in self.report["env"].items():
            if "${{" not in str(value):
                env[name] = str(value)
            for key, wired in self.deploy_job.get("outputs", {}).items():
                if value == f"${{{{ needs.deploy.outputs.{key} }}}}":
                    step_output = wired.removeprefix("${{ steps.deploy.outputs.").removesuffix(" }}")
                    env[name] = outputs.get(step_output, "")
        done = self.run_step(
            self.report,
            DEPLOYMENT="5",
            COMMIT=commit,
            RESULT=result,
            RUN_URL=f"{SERVER}/{REPOSITORY}/actions/runs/1",
            GH_TOKEN="example-token",
            ACTIONS_TOKEN="example-actions-token",
            **env,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        # One status, which json.loads refuses to read as more.
        return json.loads((self.fake / "posted").read_text()), (self.fake / "summary").read_text()

    def ended(self, exit_status):
        code, outputs = self.deploy_ends(exit_status)
        return code, self.reported(outputs, "success" if code == 0 else "failure")

    def run_record(self, result, *deploy_steps):
        """GitHub's record of this run's jobs: the Deploy job, named as the workflow names it,
        ended ``result`` with ``deploy_steps`` (the deploy step's conclusion), none for a job no
        runner took."""
        job_steps = [step("Set up job", 1, "success")]
        job_steps += [step(self.deploy["name"], 2, conclusion) for conclusion in deploy_steps]
        listed = [
            {"id": 77, "run_id": 1, "name": "Approve", "status": "completed", "conclusion": "success", "steps": []},
            {
                "id": 78,
                "run_id": 1,
                "name": self.deploy_job["name"],
                "status": "completed",
                "conclusion": result,
                "steps": job_steps if deploy_steps else [],
            },
        ]
        (self.fake / "jobs.json").write_text(json.dumps({"total_count": len(listed), "jobs": listed}))

    def test_each_exit_status_is_reported_as_its_outcome_declares(self):
        for name, outcome in self.outcomes.items():
            if outcome["exit"] is None:
                continue
            with self.subTest(outcome=name):
                code, (posted, _) = self.ended(outcome["exit"])
                # A release that stands with a check failed still fails the job.
                self.assertEqual(code, outcome["exit"])
                self.assertEqual((posted["state"], posted["description"]), (outcome["state"], outcome["description"]))

        descriptions = [outcome["description"] for outcome in self.outcomes.values()]
        self.assertEqual(len(set(descriptions)), len(descriptions), descriptions)

    def test_a_deploy_hq_host_never_answered_is_reported_by_whether_its_step_began(self):
        # (the deploy job's result, its deploy step's conclusion or None for none, the outcome)
        cases = [
            ("skipped", None, "not-approved"),
            # Pending behind a newer deploy, or waiting for the runner, when it was cancelled.
            ("cancelled", None, "not-approved"),
            ("cancelled", "skipped", "not-approved"),
            ("failure", None, "not-deployed"),
            # Cancelled by a person or the job's timeout while hq-host ran.
            ("cancelled", "cancelled", "interrupted"),
            # The runner was lost while hq-host ran.
            ("failure", "failure", "interrupted"),
        ]
        for result, conclusion, name in cases:
            with self.subTest(result=result, step=conclusion):
                self.run_record(result, *([conclusion] if conclusion else []))
                posted, summary = self.reported({}, result)
                outcome = self.outcomes[name]
                self.assertEqual((posted["state"], posted["description"]), (outcome["state"], outcome["description"]))
                if outcome["production"] == "unknown":
                    self.assertIn("hq-host verify", summary)

        reached = {name for _, _, name in cases}
        never_answered = {name for name, outcome in self.outcomes.items() if outcome["exit"] is None}
        self.assertLessEqual(never_answered, reached)

    def test_a_deploy_whose_step_was_not_read_never_claims_the_previous_image(self):
        # No record of the run's jobs: GitHub refused the read.
        posted, _ = self.reported({}, "cancelled")
        (outcome,) = [each for each in self.outcomes.values() if each["description"] == posted["description"]]
        self.assertEqual(outcome["production"], "unknown")
        self.assertNotEqual(posted["state"], "success")

    def test_a_status_declared_nowhere_is_not_deployed(self):
        exits = {outcome["exit"] for outcome in self.outcomes.values()}
        undeclared = next(code for code in range(1, 126) if code not in exits)
        _, (posted, _) = self.ended(undeclared)
        self.assertEqual(posted["state"], self.outcomes["not-deployed"]["state"])
        self.assertEqual(self.outcomes["not-deployed"]["production"], "previous")

    def test_a_deploy_whose_rollback_failed_says_production_s_state_is_unknown(self):
        (failed,) = [
            outcome
            for outcome in self.outcomes.values()
            if outcome["production"] == "unknown" and outcome["exit"] is not None
        ]
        self.assertNotIn(failed["exit"], (0, 1, 2))

        code, (posted, summary) = self.ended(failed["exit"])

        self.assertNotEqual(code, 0)
        self.assertEqual(posted["state"], "error")
        self.assertIn("unknown", posted["description"])
        self.assertNotIn("previous image", posted["description"])
        self.assertNotIn("cosign verify", summary)
        self.assertIn("/var/log/severino-hq", summary)

    def test_a_live_release_links_its_changes_and_how_to_verify_its_image(self):
        live = [outcome["exit"] for outcome in self.outcomes.values() if outcome["production"] == "release"]
        for exit_status in live:
            with self.subTest(exit_status=exit_status):
                _, (_, summary) = self.ended(exit_status)
                self.assertIn(f"{SERVER}/{REPOSITORY}/compare/{BEFORE}...{RELEASE}", summary)
                (verify,) = [line for line in summary.splitlines() if line.startswith("cosign verify ")]
                self.assertIn("--certificate-oidc-issuer https://token.actions.githubusercontent.com", verify)
                self.assertIn(
                    f"--certificate-identity {SERVER}/{REPOSITORY}/.github/workflows/compose.yml@refs/heads/main",
                    verify,
                )
                self.assertTrue(verify.endswith(f" {IMAGE}"))

        _, (_, summary) = self.ended(self.outcomes["not-deployed"]["exit"])
        self.assertNotIn("cosign verify", summary)
        self.assertNotIn("/compare/", summary)

    def test_a_release_whose_check_failed_is_the_one_the_next_is_compared_with(self):
        _, (posted, _) = self.ended(self.outcomes["live-check-failed"]["exit"])
        self.history(
            {
                2: (BEFORE, "2026-10-25T00:36:00Z", [status("inactive"), status("success")]),
                5: (RELEASE, "2026-10-31T02:19:12Z", [posted]),
                6: (NEWER, "2026-10-31T05:00:00Z", [status("in_progress")]),
            }
        )

        _, summary = self.reported({"status": "0"}, "success", commit=NEWER)

        self.assertIn(f"/compare/{RELEASE}...{NEWER}", summary)
