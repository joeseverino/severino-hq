"""GitHub delivery as HQ declares it, and the workflows it follows.

The controller (controller/providers/github_delivery.go) reads and reports
delivery; its tests hold the check name, the comment marker and the app's
registered permissions.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from hq.domains.control_plane.providers import PROVIDERS

from .. import github


class AdoptionTests(SimpleTestCase):
    def test_adoption_takes_only_a_current_record(self):
        record = {"repository": "example/host", "workflow": github.COMPOSE_WORKFLOW, "branch": "main", "extensions": []}
        provider = PROVIDERS[github.KIND]
        self.assertFalse(provider.adopts({**record, "production": "alpha bbbbbbb is admitted"}))
        self.assertTrue(provider.adopts({**record, "production": github.CURRENT}))


class PipelineReportTests(SimpleTestCase):
    """The workflows say what HQ's code says, and keep what they must apart."""

    def read(self, *parts):
        from pathlib import Path

        return Path(__file__).resolve().parents[5].joinpath(*parts).read_text()

    def test_the_review_is_one_check_by_one_name(self):
        name = '"Severino HQ · Review"'

        self.assertIn(name, self.read(".github", "workflows", "ci.yml"))
        self.assertIn(f"check_name={name}", self.read("scripts", "hq-verdict.sh"))

    def test_hqs_key_never_reaches_the_homelab_runner(self):
        import re

        deploy = self.read(".github", "workflows", "deploy.yml")
        job = re.search(r"\n  deploy:\n(.*?)(?=\n  [a-z]+:\n|\Z)", deploy, re.S).group(1)

        self.assertIn("self-hosted", job)
        self.assertNotIn("HQ_APP_KEY", job)

    def test_only_deploy_runs_on_the_homelab_and_no_pull_request_starts_it(self):
        import re
        from pathlib import Path

        workflows = Path(__file__).resolve().parents[5] / ".github" / "workflows"
        hosting = sorted(
            path.name for path in workflows.glob("*.yml")
            if re.search(r"^\s*runs-on:.*self-hosted", path.read_text(), re.M)
        )
        deploy = self.read(".github", "workflows", "deploy.yml")
        triggers = deploy.split("\non:\n", 1)[1].split("\n\n", 1)[0]

        self.assertEqual(hosting, ["deploy.yml"])
        self.assertNotIn("pull_request", triggers)

    def test_each_step_starts_the_next_and_names_the_commit(self):
        from pathlib import Path

        workflows = Path(__file__).resolve().parents[5] / ".github" / "workflows"
        # workflow_run fires for any run of the workflow it watches, a pull
        # request's included, so it can start a deploy of the wrong commit.
        self.assertEqual([path.name for path in workflows.glob("*.yml") if "workflow_run:" in path.read_text()], [])
        self.assertIn('gh workflow run compose.yml', self.read(".github", "workflows", "ci.yml"))
        self.assertIn('-f commit="$COMMIT"', self.read(".github", "workflows", "ci.yml"))
        self.assertIn('gh workflow run deploy.yml', self.read(".github", "workflows", "compose.yml"))
        self.assertIn('-f commit="$COMMIT"', self.read(".github", "workflows", "compose.yml"))
