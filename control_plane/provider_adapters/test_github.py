"""Continuous delivery against GitHub's documented response shapes."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase

from control_plane.providers import PROVIDERS

from . import github, github_app

HOST = "example/host"
EXT = "example/alpha"
RUNNING = "a" * 40
ADMITTED = "b" * 40


class GitHub:
    """A controller runtime answering as GitHub would, recording every call."""

    def __init__(
        self,
        *,
        admitted=RUNNING,
        compose_runs=(),
        deploy_runs=(),
        dispatched=None,
        checks=None,
        pulls=(),
        comments=(),
    ):
        self.admitted = admitted
        self.compose_runs = list(compose_runs)
        self.deploy_runs = list(deploy_runs)
        self.dispatched = dispatched
        self.checks = dict(checks or {})
        self.pulls = list(pulls)
        self.comments = list(comments)
        self.calls: list[tuple[str, str, Any]] = []
        self.minted: list[dict[str, Any]] = []
        self.signed: list[bytes] = []

    # ProviderRuntime
    def connection_prefix(self, provider, connection_ref=""):
        return "GITHUB"

    def required(self, prefix, name):
        return {"APP_ID": "12345", "CONNECTION_REF": "github"}[name]

    def snapshot_value(self, key, load):
        return load()

    def condition(self, condition_type, status, reason, message):
        return {"type": condition_type, "status": status, "reason": reason, "message": message}

    def composition(self):
        return {
            "repository": HOST,
            "image": "ghcr.io/example/host/composition@sha256:" + "c" * 64,
            "extensions": (
                {
                    "plugin": "example.alpha",
                    "source_repository": EXT,
                    "source_workflow": ".github/workflows/admit-plugin.yml",
                    "source_commit": RUNNING,
                },
            ),
        }

    def sign(self, connection_ref, data):
        assert connection_ref == "github"
        self.signed.append(data)
        return b"signature"

    def signing_public_key(self, connection_ref):
        return PUBLIC

    def request(self, url, *, method="GET", headers=None, payload=None):
        path = url.removeprefix(github_app.API)
        self.calls.append((method, path, payload))
        if path.endswith("/installation"):
            return {"id": 7}
        if path == "/app/installations/7/access_tokens":
            self.minted.append(payload)
            return {"token": f"token-{len(self.minted)}"}
        if "/actions/workflows/admit-plugin.yml/runs" in path:
            return {"workflow_runs": [{"head_sha": self.admitted, "updated_at": "2026-09-26T10:00:00Z"}]}
        if "/actions/workflows/compose.yml/runs" in path:
            return {"workflow_runs": self.compose_runs}
        if "/actions/workflows/deploy.yml/runs" in path:
            return {"workflow_runs": self.deploy_runs}
        if "/check-runs?" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            found = self.checks.get(sha)
            return {"check_runs": [found] if found else []}
        if path.endswith("/dispatches"):
            return self.dispatched
        if path.endswith("/pulls"):
            return self.pulls
        if path.endswith("/comments?per_page=100"):
            return self.comments
        return None

    def writes(self):
        return [(method, path, payload) for method, path, payload in self.calls if method != "GET"
                and "/access_tokens" not in path]


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _key()
PUBLIC = (
    KEY.public_key()
    .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    .decode()
)
SPEC = {
    "repository": HOST,
    "workflow": github.COMPOSE_WORKFLOW,
    "branch": "main",
    "production": github.CURRENT,
}


def run(status, conclusion=None, number=99):
    return {
        "id": number,
        "status": status,
        "conclusion": conclusion,
        "created_at": "2026-09-26T10:01:00Z",
        "html_url": f"https://github.com/{HOST}/actions/runs/{number}",
    }


class DeliveryTests(SimpleTestCase):
    def test_current_production_writes_nothing(self):
        hub = GitHub()

        result = github.reconcile(hub, dict(SPEC))

        self.assertEqual(result.status["production"], github.CURRENT)
        self.assertEqual(hub.writes(), [])
        self.assertFalse(result.changed)

    def test_an_admission_with_no_composition_yet_is_reported_and_nothing_is_started(self):
        hub = GitHub(admitted=ADMITTED)

        result = github.reconcile(hub, dict(SPEC))

        (check,) = hub.writes()
        self.assertEqual(check[1], "/repos/example/alpha/check-runs")
        self.assertEqual(check[2]["head_sha"], ADMITTED)
        self.assertEqual(check[2]["status"], "queued")
        self.assertEqual(check[2]["output"]["title"], "Waiting for its composition")
        self.assertIn("no composition has started", result.status["production"])

    def test_hq_never_starts_a_workflow(self):
        # The admission dispatches the composition itself; HQ only reports,
        # so its app is never granted Actions write on any repository.
        for hub in (GitHub(admitted=ADMITTED), GitHub(admitted=ADMITTED, compose_runs=[run("waiting")]),
                    GitHub(admitted=ADMITTED, compose_runs=[run("completed", "failure")])):
            github.reconcile(hub, dict(SPEC))

            self.assertFalse(any(path.endswith("/dispatches") for _, path, _ in hub.writes()))
            self.assertFalse(any(item["permissions"].get("actions") == "write" for item in hub.minted))

    def test_each_token_is_minted_for_one_call_alone(self):
        hub = GitHub(admitted=ADMITTED)

        github.reconcile(hub, dict(SPEC))

        grants = {(tuple(item["repositories"]), tuple(item["permissions"].items())) for item in hub.minted}
        self.assertIn((("alpha",), (("checks", "write"),)), grants)
        self.assertTrue(all(len(item["permissions"]) == 1 for item in hub.minted))

    def test_a_running_composition_is_reported_not_restarted(self):
        hub = GitHub(admitted=ADMITTED, compose_runs=[run("waiting")],
                     checks={ADMITTED: {"id": 5, "status": "queued"}})

        github.reconcile(hub, dict(SPEC))

        (patch,) = hub.writes()
        self.assertEqual(patch[0], "PATCH")
        self.assertEqual(patch[1], "/repos/example/alpha/check-runs/5")
        self.assertEqual(patch[2]["output"]["title"], "Waiting for deploy approval")
        self.assertEqual(patch[2]["details_url"], f"https://github.com/{HOST}/actions/runs/99")

    def test_a_run_from_before_the_admission_does_not_carry_it(self):
        earlier = {**run("completed", "success", 98), "created_at": "2026-09-26T09:00:00Z"}
        hub = GitHub(admitted=ADMITTED, compose_runs=[earlier])

        github.reconcile(hub, dict(SPEC))

        (check,) = hub.writes()
        self.assertEqual(check[2]["output"]["title"], "Waiting for its composition")

    def test_a_failed_composition_is_degraded_and_never_retried(self):
        hub = GitHub(admitted=ADMITTED, compose_runs=[run("completed", "failure")])

        result = github.reconcile(hub, dict(SPEC))

        self.assertFalse(any(path.endswith("/dispatches") for _, path, _ in hub.writes()))
        self.assertEqual(result.conditions[0]["type"], "Degraded")
        (check,) = hub.writes()
        self.assertEqual(check[2]["conclusion"], "failure")

    def test_a_live_commit_is_confirmed_and_announced_once(self):
        hub = GitHub(checks={RUNNING: {"id": 5, "status": "in_progress"}},
                     pulls=[{"number": 11, "merged_at": "2026-09-26T09:59:00Z"}])

        result = github.reconcile(hub, dict(SPEC))

        patch, comment = hub.writes()
        self.assertEqual(patch[2]["conclusion"], "success")
        self.assertEqual(comment[1], "/repos/example/alpha/issues/11/comments")
        self.assertIn(RUNNING[:12], comment[2]["body"])
        self.assertNotIn("—", comment[2]["body"])
        self.assertIn("not yet confirmed", result.status["production"])

        again = GitHub(checks={RUNNING: {"id": 5, "status": "in_progress"}},
                       pulls=[{"number": 11, "merged_at": "2026-09-26T09:59:00Z"}],
                       comments=[{"body": comment[2]["body"]}])
        github.reconcile(again, dict(SPEC))
        self.assertEqual([path for _, path, _ in again.writes()], ["/repos/example/alpha/check-runs/5"])

    def test_each_stage_reads_differently_so_each_is_acted_on_once(self):
        stages = {
            github.production(github.delivery(GitHub(admitted=ADMITTED, compose_runs=runs), SPEC))
            for runs in ([], [run("in_progress")], [run("waiting")], [run("completed", "failure")])
        }
        self.assertEqual(len(stages), 4)

    def test_adoption_takes_only_a_current_record(self):
        record = github.inventory(GitHub(admitted=ADMITTED))[0]
        provider = PROVIDERS[github.KIND]
        self.assertFalse(provider.adopts(record))
        self.assertTrue(provider.adopts(github.inventory(GitHub())[0]))


class AppTests(SimpleTestCase):
    def test_the_jwt_names_the_app_and_is_signed_by_the_runtime(self):
        hub = GitHub()

        token = github_app.app_jwt(hub)

        header, claims, signature = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
        self.assertEqual(payload["iss"], "12345")
        self.assertLessEqual(payload["exp"] - payload["iat"], 600)
        self.assertEqual(hub.signed, [f"{header}.{claims}".encode()])
        self.assertEqual(base64.urlsafe_b64decode(signature + "=="), b"signature")

    def test_the_fingerprint_is_the_one_github_lists(self):
        der = KEY.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()

        self.assertEqual(github_app.fingerprint(PUBLIC), expected)

    def test_a_token_never_spans_two_accounts(self):
        with self.assertRaisesRegex(github_app.ProviderError, "one account"):
            github_app.token(GitHub(), ("one/a", "two/b"), {"actions": "read"})


class RegistrationTests(SimpleTestCase):
    """HQ's one app is registered with exactly what its code and pipeline ask for."""

    def test_the_app_holds_exactly_what_hq_and_its_pipeline_ask_for(self):
        from pathlib import Path

        from . import github_readings

        wanted = dict(github_readings.READ)
        for name, level in github.REPORTS.items():
            wanted[name] = level
        # An admission starts the host's composition (the admit-plugin action).
        wanted["actions"] = "write"
        apps = json.loads((Path(__file__).resolve().parents[2] / "deploy" / "github-apps.json").read_text())["apps"]

        self.assertEqual(list(apps), ["hq"])
        self.assertEqual(apps["hq"]["permissions"], wanted)


class PipelineReportTests(SimpleTestCase):
    """The workflows say what HQ's code says, and keep what they must apart."""

    def read(self, *parts):
        from pathlib import Path

        return Path(__file__).resolve().parents[2].joinpath(*parts).read_text()

    def test_production_is_one_check_by_one_name(self):
        for text in (self.read(".github", "workflows", "deploy.yml"),
                     self.read(".github", "actions", "admit-plugin", "action.yml"),
                     self.read("scripts", "hq-report.sh")):
            self.assertIn(github.CHECK_NAME, text)

    def test_the_review_is_one_check_by_one_name(self):
        name = '"Severino HQ · Review"'

        self.assertIn(name, self.read(".github", "workflows", "ci.yml"))
        self.assertIn(f"check_name={name}", self.read("scripts", "hq-verdict.sh"))

    def test_the_host_comment_carries_hqs_marker(self):
        marker = github._MARKER.split("{sha}")[0]

        self.assertIn(f'marker="{marker}${{COMMIT', self.read("scripts", "hq-report.sh"))

    def test_hqs_key_never_reaches_the_homelab_runner(self):
        import re

        deploy = self.read(".github", "workflows", "deploy.yml")
        job = re.search(r"\n  deploy:\n(.*?)(?=\n  [a-z]+:\n|\Z)", deploy, re.S).group(1)

        self.assertIn("self-hosted", job)
        self.assertNotIn("HQ_APP_KEY", job)

    def test_only_deploy_runs_on_the_homelab_and_no_pull_request_starts_it(self):
        import re
        from pathlib import Path

        workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
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

        workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
        # workflow_run fires for any run of the workflow it watches, a pull
        # request's included, which once started a deploy of the wrong commit.
        self.assertEqual([path.name for path in workflows.glob("*.yml") if "workflow_run:" in path.read_text()], [])
        self.assertIn('gh workflow run compose.yml', self.read(".github", "workflows", "ci.yml"))
        self.assertIn('-f commit="$COMMIT"', self.read(".github", "workflows", "ci.yml"))
        self.assertIn('gh workflow run deploy.yml', self.read(".github", "workflows", "compose.yml"))
        self.assertIn('-f commit="$COMMIT"', self.read(".github", "workflows", "compose.yml"))

    def test_a_stage_names_the_workflow_it_is_in(self):
        waiting = {**run("waiting"), "name": "Deploy"}

        stage = github.production(github.delivery(GitHub(admitted=ADMITTED, deploy_runs=[waiting]), SPEC))

        self.assertIn("Deploy run 99 is waiting for deploy approval", stage)
