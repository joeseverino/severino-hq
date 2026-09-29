from __future__ import annotations

from django.test import SimpleTestCase

from controller_runtime import provider_http

from . import github_app, github_readings
from .contracts import PERMISSION_REFUSAL, ProviderError
from .parts import part_ledger

REPO = "example/alpha"


class GitHub:
    """GitHub as its documented responses shape it, for one installation."""

    def __init__(self, *, refuse=()):
        self.minted: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.refuse = refuse

    def connection_prefix(self, provider, connection_ref=""):
        return "GITHUB"

    def required(self, prefix, name):
        return {"APP_ID": "12345", "CONNECTION_REF": "github"}[name]

    def snapshot_value(self, key, load):
        return load()

    def sign(self, ref, data):
        return b"signature"

    def composition(self):
        return {"repository": REPO, "image": f"ghcr.io/{REPO}/composition@sha256:{'a' * 64}"}

    def request(self, url, *, method="GET", headers=None, payload=None):
        path = url.removeprefix(github_app.API)
        self.calls.append((method, path))
        if url.startswith("https://ghcr.io/token"):
            self.registry_auth = (headers or {}).get("Authorization", "")
            return {"token": "r"}
        if url.startswith("https://ghcr.io/v2/"):
            return {"tags": ["latest", "sha-1234", f"sha256-{'a' * 64}.sig"]}
        if path == "/app/installations":
            return [{"id": 7}]
        if path == f"/repos/{REPO}/installation":
            return {"id": 7}
        if path.endswith("/access_tokens"):
            self.minted.append(payload)
            return {"token": "t"}
        if path.startswith("/installation/repositories"):
            return {"repositories": [{"full_name": REPO}]}
        for refused in self.refuse:
            if refused in path:
                raise ProviderError("Resource not accessible", refusal=PERMISSION_REFUSAL)
        base = f"/repos/{REPO}"
        answers = {
            base: {"private": True, "html_url": f"https://github.com/{REPO}", "default_branch": "main"},
            f"{base}/commits/main": {"sha": "abc1234def", "html_url": "u", "commit": {"message": "Fix it\n\nbody", "committer": {"date": "2026-09-26T00:00:00Z"}}, "author": {"login": "someone"}},
            f"{base}/commits/abc1234def/check-runs?per_page=100": {"check_runs": [
                {"name": "lint", "status": "completed", "conclusion": "failure"},
                {"name": "test", "status": "completed", "conclusion": "success"},
            ]},
            f"{base}/pulls?state=open&per_page=30": [{"number": 3, "title": "A change", "user": {"login": "someone"}}],
            f"{base}/actions/runs?per_page=50": {"workflow_runs": [
                {"id": 9, "name": "Compose", "status": "waiting", "head_sha": "abc1234def", "created_at": "2026-09-26T00:00:00Z"},
                {"id": 8, "name": "Compose", "status": "completed", "conclusion": "success"},
                {"id": 5, "name": "Update pip #1", "event": "dynamic", "status": "completed"},
            ]},
            f"{base}/actions/runs/9/pending_deployments": [{"environment": {"name": "production"}}],
            f"{base}/releases?per_page=1": [],
            f"{base}/deployments?per_page=20": [{"id": 4, "environment": "production", "sha": "abc1234def"}],
            f"{base}/deployments/4/statuses?per_page=1": [{"state": "success", "log_url": f"https://github.com/{REPO}/actions/runs/5/job/77"}],
            f"{base}/actions/jobs/77": {"steps": [
                {"name": "Install Cosign", "conclusion": "success"},
                {"name": "Verify the image was signed", "conclusion": "success"},
            ]},
            f"{base}/code-scanning/alerts?state=open&per_page=100": [{"rule": {"security_severity_level": "high"}}],
            f"{base}/dependabot/alerts?state=open&per_page=100": [],
            f"{base}/secret-scanning/alerts?state=open&per_page=100": [],
            f"{base}/actions/artifacts?per_page=50": {"artifacts": [
                {"name": "alpha-admission-abc", "expires_at": "2026-12-25T00:00:00Z", "expired": False},
                {"name": "old", "expires_at": "2026-01-01T00:00:00Z", "expired": True},
            ]},
        }
        return answers.get(path)


class RepositoryReadingTests(SimpleTestCase):
    def read(self, hub):
        with part_ledger() as refused:
            records = github_readings.read_repositories(hub)
        return records, refused

    def test_it_reads_each_repository_the_installation_covers(self):
        (record,), refused = self.read(GitHub())

        self.assertEqual(record["repository"], REPO)
        self.assertEqual(record["head"]["message"], "Fix it")
        self.assertEqual((record["checks"]["state"], record["checks"]["failing"]), ("failure", ["lint"]))
        self.assertEqual([run["name"] for run in record["runs"]], ["Compose"])  # newest, no dynamic runs
        self.assertEqual(record["waiting"][0]["environments"], ["production"])
        (deployed,) = record["deployments"]
        self.assertEqual((deployed["environment"], deployed["state"]), ("production", "success"))
        # Only the steps that verify, from the job the deployment names.
        self.assertEqual(deployed["verified"], [{"name": "Verify the image was signed", "conclusion": "success"}])
        self.assertEqual(record["alerts"]["code_scanning"], {"high": 1})
        self.assertEqual([item["name"] for item in record["artifacts"]], ["alpha-admission-abc"])
        self.assertEqual(refused, [])

    def test_every_call_is_under_a_read_only_token_for_that_repository_alone(self):
        hub = GitHub()
        self.read(hub)

        scoped = [grant for grant in hub.minted if "repositories" in grant]
        self.assertTrue(scoped)
        for grant in scoped:
            self.assertEqual(grant["repositories"], ["alpha"])
            self.assertEqual(set(grant["permissions"].values()), {"read"})
        self.assertEqual([method for method, path in hub.calls if method != "GET" and not path.endswith("/access_tokens")], [])

    def test_an_alert_kind_github_refuses_is_that_part_refused_not_zero(self):
        (record,), refused = self.read(GitHub(refuse=("/code-scanning/",)))

        self.assertNotIn("code_scanning", record["alerts"])
        self.assertEqual([(item["part"], item["scope"]) for item in refused], [("code_scanning", REPO)])

    def test_a_403_under_a_token_that_holds_the_permission_is_never_a_missing_permission(self):
        """GitHub will not mint a token for a permission the installation
        lacks, so the refusal is the repository's: a feature it does not offer."""

        (_,), refused = self.read(GitHub(refuse=("/code-scanning/", "/rules/branches/")))

        self.assertEqual({item["refusal"] for item in refused}, {""})
        self.assertTrue(all(item["reason"].startswith("Not offered on this repository") for item in refused))


class WorkflowTests(SimpleTestCase):
    def test_each_workflow_that_exists_now_once_under_its_name_now(self):
        runs = [
            {"id": 3, "workflow_id": 1, "name": "CI", "status": "completed", "conclusion": "success"},
            {"id": 2, "workflow_id": 1, "name": "ci", "status": "completed", "conclusion": "failure"},
            {"id": 1, "workflow_id": 2, "name": "dependency review", "status": "completed"},
        ]
        workflows = [
            {"id": 1, "name": "CI", "state": "active"},
            {"id": 2, "name": "dependency review", "state": "deleted"},
        ]

        found = github_readings._latest_runs(runs, workflows)

        self.assertEqual([(run["name"], run["conclusion"]) for run in found], [("CI", "success")])

    def test_without_a_workflow_list_every_named_workflow_still_shows(self):
        runs = [{"id": 1, "workflow_id": 1, "name": "CI", "status": "completed"}]

        self.assertEqual([run["name"] for run in github_readings._latest_runs(runs, None)], ["CI"])


class ImageTests(SimpleTestCase):
    def test_the_composition_the_controller_runs_is_read_with_its_signatures(self):
        hub = GitHub()
        with part_ledger():
            (record,) = github_readings.read_repositories(hub)

        images = {image["name"]: image for image in record["images"]}
        self.assertEqual(set(images), {"example/alpha", "example/alpha/composition"})
        self.assertEqual(images["example/alpha/composition"]["signed"], ["a" * 64])
        self.assertEqual(images["example/alpha/composition"]["tags"], 2)
        # The registry is asked as the App, with the token it minted.
        self.assertTrue(hub.registry_auth.startswith("Basic "))


class ImageRefusalTests(SimpleTestCase):
    def test_an_image_the_app_may_not_read_is_refused_alone(self):
        class Refusing(GitHub):
            def request(self, url, **kwargs):
                if "composition/tags" in url:
                    raise ProviderError("403", refusal=PERMISSION_REFUSAL)
                return super().request(url, **kwargs)

        with part_ledger() as refused:
            (record,) = github_readings.read_repositories(Refusing())

        self.assertEqual([image["name"] for image in record["images"]], ["example/alpha"])
        self.assertEqual([item["scope"] for item in refused], ["example/alpha:example/alpha/composition"])


FLEET = tuple(f"example/r{index}" for index in range(5))


class Sweeping(GitHub):
    """The same GitHub, installed on five repositories, behind the controller's
    own per-sweep snapshot rather than a stand-in for it."""

    def snapshot_value(self, key, load):
        return provider_http.snapshot_value(key, load)

    def request(self, url, *, method="GET", headers=None, payload=None):
        path = url.removeprefix(github_app.API)
        if path.startswith("/installation/repositories"):
            self.calls.append((method, path))
            return {"repositories": [{"full_name": name} for name in FLEET]}
        if path.startswith("/repos/") and path.endswith("/installation"):
            self.calls.append((method, path))
            return {"id": 7}
        return super().request(url, method=method, headers=headers, payload=payload)

    def count(self, what):
        kinds = {
            "mint": lambda path: path.endswith("/access_tokens"),
            "lookup": lambda path: path.startswith("/repos/") and path.endswith("/installation"),
        }
        return sum(1 for _, path in self.calls if kinds[what](path))


class SweepTokenTests(SimpleTestCase):
    """A token is minted once per scope per sweep, and never broader or older."""

    def sweep(self, hub, *calls):
        with provider_http.provider_snapshot():
            for repositories, permissions in calls:
                github_app.call(hub, "/rate_limit", repositories=repositories, permissions=permissions)

    def test_repeated_calls_of_one_scope_in_a_sweep_mint_once(self):
        hub = Sweeping()
        self.sweep(hub, *[([REPO], {"contents": "read"})] * 4)

        self.assertEqual(hub.count("mint"), 1)
        self.assertEqual(hub.minted, [{"repositories": ["alpha"], "permissions": {"contents": "read"}}])
        self.assertEqual(sum(1 for _, path in hub.calls if path == "/rate_limit"), 4)

    def test_a_different_scope_gets_its_own_token_never_a_broader_one(self):
        hub = Sweeping()
        self.sweep(
            hub,
            (["example/alpha"], {"contents": "read"}),
            (["example/beta"], {"contents": "read"}),
            (["example/alpha"], {"contents": "read", "checks": "read"}),
            (["example/alpha", "example/beta"], {"contents": "read"}),
            (["example/alpha"], {"contents": "read"}),
        )

        self.assertEqual(
            hub.minted,
            [
                {"repositories": ["alpha"], "permissions": {"contents": "read"}},
                {"repositories": ["beta"], "permissions": {"contents": "read"}},
                {"repositories": ["alpha"], "permissions": {"checks": "read", "contents": "read"}},
                {"repositories": ["alpha", "beta"], "permissions": {"contents": "read"}},
            ],
        )

    def test_a_token_is_not_reused_by_the_next_sweep(self):
        hub = Sweeping()
        self.sweep(hub, ([REPO], {"contents": "read"}))
        self.sweep(hub, ([REPO], {"contents": "read"}))

        self.assertEqual(hub.count("mint"), 2)

    def test_outside_a_sweep_every_call_mints(self):
        hub = Sweeping()
        for _ in range(3):
            github_app.call(hub, "/rate_limit", repositories=[REPO], permissions={"contents": "read"})

        self.assertEqual(hub.count("mint"), 3)

    def test_a_sweep_of_five_repositories_mints_one_token_each(self):
        """The measured cost: one metadata token to list the installation, then
        one read token per repository, and no per-repository installation
        lookup, because the listing already said which installation it is."""

        hub = Sweeping()
        with provider_http.provider_snapshot(), part_ledger():
            records = github_readings.read_repositories(hub)

        self.assertEqual([record["repository"] for record in records], list(FLEET))
        self.assertEqual(hub.count("mint"), 1 + len(FLEET))
        self.assertEqual(hub.count("lookup"), 0)
        scoped = [grant for grant in hub.minted if "repositories" in grant]
        self.assertEqual([grant["repositories"] for grant in scoped], [[name.split("/")[1]] for name in FLEET])
        self.assertTrue(all(grant["permissions"] == github_readings.READ for grant in scoped))

    def test_without_the_snapshot_the_same_reading_mints_per_call(self):
        """What the snapshot saves, counted against the same reading."""

        hub = Sweeping()
        with part_ledger():
            github_readings.read_repositories(hub)

        api = [path for _, path in hub.calls if path.startswith("/repos/") and not path.endswith("/installation")]
        self.assertEqual(hub.count("mint"), 1 + len(api))
        self.assertEqual(hub.count("lookup"), len(api))
