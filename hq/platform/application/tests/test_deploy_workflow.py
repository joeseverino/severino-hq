"""Deploy: production is reached through one approval, by a runner that holds nothing worth taking."""

import json
import subprocess
from pathlib import Path
from typing import Any

from django.test import SimpleTestCase, tag

from .test_pipeline import WORKFLOWS, Node, code, jobs, needed, script, steps, text, upstream, workflow, workflows


@tag("source")
class DeployTests(SimpleTestCase):
    """Production is reached through one approval, by a runner that holds nothing worth taking."""

    def setUp(self):
        self.jobs = jobs(workflow("deploy.yml"))

    def needs(self, job: str) -> set[str]:
        return upstream(job, {name: needed(each) for name, each in self.jobs.items()})

    def environment(self, job: str) -> str:
        """The environment ``job`` waits on, by name."""

        declared = self.jobs[job].get("environment", "")
        return declared.get("name", "") if isinstance(declared, dict) else declared

    def test_every_job_on_the_homelab_waits_for_an_approval(self):
        homelab = [name for name, job in self.jobs.items() if "self-hosted" in job.get("runs-on", [])]
        self.assertTrue(homelab)
        for name in homelab:
            with self.subTest(job=name):
                gated = {name, *self.needs(name)}
                self.assertTrue(any(self.environment(job) == "release" for job in gated))
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
        self.assertTrue(any(self.environment(name) for name in self.jobs))
        for name in self.jobs:
            with self.subTest(job=name):
                self.assertNotEqual(self.environment(name), "production")
        (record,) = [step for step in steps(self.jobs["resolve"]) if '/deployments"' in script(step)]
        self.assertEqual(record["env"]["COMMIT"], "${{ steps.find.outputs.commit }}")
        self.assertIn('--arg ref "$COMMIT"', script(record))
        self.assertIn("ref: $ref", script(record))

    def release_check(self) -> tuple[int, list[Node]]:
        """Resolve's steps, and where among them release's protection is read."""

        resolve = steps(self.jobs["resolve"])
        (check,) = [i for i, step in enumerate(resolve) if "deployment_protection_rules" in script(step)]
        return check, resolve

    def test_release_is_checked_before_anything_is_recorded_or_approved(self):
        """GitHub makes an environment on first use, unprotected: Resolve, which
        Approve needs, reads the environment Approve waits on before it records
        anything, with the job's token and Actions read alone."""
        from hq.platform.application.github_posture import REVIEW_APP_ID

        self.assertIn("resolve", self.needs("approve"))
        check, resolve = self.release_check()
        (record,) = [i for i, step in enumerate(resolve) if '/deployments"' in script(step)]
        self.assertLess(check, record)
        self.assertIn(f"/environments/{self.environment('approve')}", script(resolve[check]))
        self.assertEqual(resolve[check]["env"]["REVIEW_APP"], f"${{{{ secrets.{REVIEW_APP_ID} }}}}")
        self.assertEqual(resolve[check]["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(self.jobs["resolve"]["permissions"].get("actions"), "read")

    def test_release_passes_only_when_main_alone_waits_on_the_review_app(self):
        """The check, run against GitHub's answers as its REST schemas give them."""
        import tempfile

        check, resolve = self.release_check()
        release = "repos/example/host/environments/release"
        main_only = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        branches = {"total_count": 1, "branch_policies": [{"id": 1, "node_id": "b1", "name": "main", "type": "branch"}]}

        def rule(app: int, enabled: bool = True) -> dict[str, Any]:
            return {"id": app, "node_id": f"r{app}", "enabled": enabled, "app": {"id": app, "slug": f"example-{app}"}}

        def answers(**changed: Any) -> dict[str, Any]:
            found = {
                release: main_only,
                f"{release}/deployment-branch-policies": branches,
                f"{release}/deployment_protection_rules": {
                    "total_count": 1,
                    "custom_deployment_protection_rules": [rule(4242)],
                },
            }
            return {path: value for path, value in {**found, **changed}.items() if value is not None}

        cases = {
            "as decided": (answers(), True),
            "no environment yet": (answers(**{release: None}), False),
            "every branch may deploy": (answers(**{release: {"deployment_branch_policy": None}}), False),
            "protected branches": (
                answers(
                    **{
                        release: {
                            "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}
                        }
                    }
                ),
                False,
            ),
            "another branch beside main": (
                answers(
                    **{
                        f"{release}/deployment-branch-policies": {
                            "total_count": 2,
                            "branch_policies": [
                                *branches["branch_policies"],
                                {"id": 2, "node_id": "b2", "name": "next", "type": "branch"},
                            ],
                        }
                    }
                ),
                False,
            ),
            "no rule": (
                answers(
                    **{
                        f"{release}/deployment_protection_rules": {
                            "total_count": 0,
                            "custom_deployment_protection_rules": [],
                        }
                    }
                ),
                False,
            ),
            "the review App's rule disabled": (
                answers(
                    **{
                        f"{release}/deployment_protection_rules": {
                            "total_count": 1,
                            "custom_deployment_protection_rules": [rule(4242, enabled=False)],
                        }
                    }
                ),
                False,
            ),
            "another App's rule": (
                answers(
                    **{
                        f"{release}/deployment_protection_rules": {
                            "total_count": 1,
                            "custom_deployment_protection_rules": [rule(99)],
                        }
                    }
                ),
                False,
            ),
        }
        for name, (answered, passes) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as scratch:
                stub = Path(scratch) / "bin"
                stub.mkdir()
                served = Path(scratch) / "served.json"
                served.write_text(json.dumps(answered))
                # gh api PATH [--jq EXPR]: the answer for PATH, or a refusal for a path GitHub would answer 404.
                gh = stub / "gh"
                gh.write_text(
                    '#!/bin/sh\nanswer=$(jq -ce --arg p "$2" \'.[$p]\' "$SERVED") || { echo "HTTP 404" >&2; exit 1; }\n'
                    'if [ "${3:-}" = --jq ]; then printf \'%s\' "$answer" | jq -r "$4"; else printf \'%s\\n\' "$answer"; fi\n'
                )
                gh.chmod(0o755)
                environ = {"PATH": f"{stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "SERVED": str(served)}
                environ |= {"GITHUB_REPOSITORY": "example/host", "REVIEW_APP": "4242", "GH_TOKEN": "example"}
                done = subprocess.run(
                    ["bash", "-c", script(resolve[check])], check=False, env=environ, capture_output=True, text=True
                )
                self.assertEqual(done.returncode == 0, passes, done.stdout + done.stderr)
                if not passes:
                    self.assertIn("mise run wire-github-app --review", done.stdout)

    def test_a_candidate_is_never_deployed(self):
        source = code((WORKFLOWS / "deploy.yml").read_text())
        self.assertNotIn("coordinated", source)
        self.assertNotIn("candidate", source)
