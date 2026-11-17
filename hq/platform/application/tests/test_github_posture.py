import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.markup_testing import hrefs, select

from ..attention import raised
from ..finding_steps import Do, Run
from ..findings import finding_key
from ..github_posture import (
    COMMANDS,
    PINS_CALLED,
    PINS_UNREAD,
    REASONS,
    STANDARD,
    postures,
)
from ..repository_findings import PROJECTS
from ..standards import INTENDED, Standing
from .test_github_estate import store

# What a well-kept repository reads as: only its owner, one read-only key in
# use, a read-only token that approves nothing, pinned actions, fixes on.


def raised_items():
    """What Projects raises on the queue: its repository rules' findings."""

    return tuple(raised(PROJECTS))


KEPT = {
    "collaborators": [{"login": "example", "role": "admin"}],
    "deploy_keys": [{"title": "deploy", "read_only": True, "last_used": timezone.now().isoformat()}],
    "token": "read",
    "token_approves_reviews": False,
    "pinning_required": True,
    "vulnerability_alerts": True,
    "security": None,
}


def kept(**changes):
    return {**KEPT, **changes}


class PostureTests(TestCase):
    def test_a_kept_private_repository_meets_the_private_standard(self):
        store(private=True, access=kept(), variables=[])

        found = postures()[0]

        self.assertEqual((found.met, found.measured, found.unmet), (8, 8, ()))
        self.assertNotIn("secret-scanning", [result.check.id for result in found.results])

    def test_a_public_repository_is_held_to_more(self):
        store(
            private=False,
            access=kept(security={"secret_scanning": "enabled", "secret_scanning_push_protection": "disabled"}),
            variables=[],
            rules={"pull_request": True, "blocks_force_push": True, "blocks_deletion": False},
            alerts={"code_scanning": {}},
        )

        found = postures()[0]

        self.assertEqual(found.state_of("secret-scanning"), Standing.MET)
        self.assertEqual(found.state_of("push-protection"), Standing.UNMET)
        self.assertEqual(found.state_of("deletion-blocked"), Standing.UNMET)
        self.assertEqual(found.state_of("code-scanning"), Standing.MET)
        self.assertEqual(len(found.results), 14)

    def test_what_hq_could_not_read_is_unavailable_not_a_failure(self):
        store(private=False)

        found = postures()[0]

        self.assertEqual(found.state_of("only-you"), Standing.UNAVAILABLE)
        self.assertEqual(found.state_of("secret-scanning"), Standing.UNAVAILABLE)
        self.assertEqual((found.measured, raised_items()), (0, ()))

    def test_someone_gaining_reach_is_serious_and_drift_is_not(self):
        stale = (timezone.now() - timedelta(days=200)).isoformat()
        store(
            private=True,
            variables=["DEPLOY_TARGET"],
            access=kept(
                collaborators=[{"login": "example", "role": "admin"}, {"login": "guest", "role": "write"}],
                deploy_keys=[{"title": "old", "read_only": False, "last_used": stale}],
                token="write",
            ),
        )

        items = {finding_key(item): item.severity for item in raised_items()}

        self.assertEqual(
            items,
            {
                "finding:github-posture::only-you": "serious",
                "finding:github-posture::keys-read-only": "serious",
                "finding:github-posture::keys-in-use": "attention",
                "finding:github-posture::token-read-only": "attention",
                "finding:github-posture::no-variables": "attention",
            },
        )

    def test_one_item_per_gap_however_many_repositories_miss_it(self):
        from hq.domains.control_plane.models import ProviderInventory

        record = {
            "connection_ref": "github",
            "default_branch": "main",
            "head": {},
            "private": True,
            "access": kept(pinning_required=False),
            "variables": [],
        }
        ProviderInventory.objects.update_or_create(
            kind="github.repository",
            defaults={
                "records": [
                    {**record, "repository": f"example/{name}", "url": f"https://github.com/example/{name}"}
                    for name in ("alpha", "beta", "gamma")
                ],
                "reachable": True,
                "connected": True,
                "observed_at": timezone.now(),
            },
        )

        (item,) = raised_items()

        self.assertEqual((finding_key(item), item.count), ("finding:github-posture::actions-pinned", 3))
        self.assertTrue(item.explanation.startswith("alpha, beta, gamma."))

    def test_an_archived_repository_is_held_to_nothing(self):
        from hq.domains.control_plane.models import ProviderInventory

        record = {
            "connection_ref": "github",
            "default_branch": "main",
            "head": {},
            "private": True,
            "access": kept(vulnerability_alerts=False),
            "variables": [],
        }
        ProviderInventory.objects.update_or_create(
            kind="github.repository",
            defaults={
                "records": [
                    {
                        **record,
                        "repository": f"example/{name}",
                        "url": f"https://github.com/example/{name}",
                        "archived": name == "retired",
                    }
                    for name in ("kept", "retired")
                ],
                "reachable": True,
                "connected": True,
                "observed_at": timezone.now(),
            },
        )

        self.assertEqual([found.subject.name for found in postures()], ["example/kept"])
        (item,) = raised_items()
        self.assertEqual((finding_key(item), item.count), ("finding:github-posture::vulnerability-alerts", 1))
        self.assertNotIn("retired", item.explanation)

    def test_an_item_is_as_old_as_the_oldest_repository_reading_it_names(self):
        from hq.domains.control_plane.models import ProviderInventory

        store(private=True, access=kept(token="write"), variables=[])
        read = timezone.now() - timedelta(hours=2)
        ProviderInventory.objects.filter(kind="github.repository").update(observed_at=read)

        (item,) = raised_items()

        self.assertEqual(item.observed_at, read)

    def test_the_build_queue_carries_both_the_repository_and_the_standard(self):
        store(private=True, checks={"state": "failure", "failing": ["lint"]}, access=kept(token="write"), variables=[])

        keys = {finding_key(item) for item in raised_items()}

        self.assertEqual(keys, {"finding:github-failing::example/alpha", "finding:github-posture::token-read-only"})


class PostureViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

    def test_the_page_leads_with_what_is_not_met(self):
        store(private=True, access=kept(token="write"), variables=["HQ_IMAGE"])

        response = self.client.get(reverse("posture"))

        checks = {check.id: check for check in STANDARD}
        gaps = [gap["label"] for gap in response.context["unmet"]]
        self.assertEqual(gaps, [checks["token-read-only"].unmet, checks["no-variables"].unmet])
        self.assertIn("only-you", [check.id for check in response.context["everywhere"]])
        self.assertContains(response, "6 of 8")
        self.assertContains(response, "HQ_IMAGE")
        # A failing check is titled by what is wrong, never by what passing is.
        titles = [element.words for element in select(response, "data-attention-title")]
        self.assertEqual(titles, gaps)
        self.assertFalse(set(titles) & {check.label for check in STANDARD})

    def test_a_repository_joins_its_project(self):
        from hq.domains.projects.models import Project

        project = Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")
        store(private=True, access=kept(), variables=[])

        response = self.client.get(reverse("posture"))

        self.assertIn(project.get_absolute_url(), hrefs(response))
        self.assertFalse(response.context["unmet"])

    def test_nothing_read_says_so(self):
        response = self.client.get(reverse("posture"))

        self.assertContains(response, "No repository read yet.")
        self.assertContains(response, "Connect GitHub App")


def runs(item) -> list[tuple[str, str]]:
    """Each command an item offers: its label and the command."""

    return [(step.label, step.command) for step in item.steps if isinstance(step, Run)]


def told(item) -> list[str]:
    """What an item says to do in words, step by step."""

    return [step.text for step in item.steps if isinstance(step, Do)]


class PostureHelpTests(TestCase):
    def item(self, key: str, **record):
        store(private=True, **{"access": kept(), "variables": [], **record})
        return {finding_key(item): item for item in raised_items()}[f"finding:github-posture::{key}"]

    def test_vulnerability_alerts_name_the_exact_command_for_the_repository(self):
        item = self.item("vulnerability-alerts", access=kept(vulnerability_alerts=False))

        self.assertEqual(runs(item), [("alpha", "gh api -X PUT repos/example/alpha/vulnerability-alerts")])

    def test_pinning_offers_no_setting_while_the_workflows_were_not_read(self):
        item = self.item("actions-pinned", access=kept(pinning_required=False))

        self.assertIn(PINS_UNREAD, item.no_help_reason)
        self.assertNotIn("sha_pinning_required", str(item.steps))

    def test_pinning_is_a_paste_per_workflow_then_the_setting(self):
        pins = [
            {
                "path": ".github/workflows/ci.yml",
                "uses": "actions/checkout@v4",
                "action": "actions/checkout",
                "ref": "v4",
                "sha": "a" * 40,
            },
            {
                "path": ".github/workflows/ci.yml",
                "uses": "actions/checkout@v4",
                "action": "actions/checkout",
                "ref": "v4",
                "sha": "a" * 40,
            },
        ]
        item = self.item("actions-pinned", access=kept(pinning_required=False), pins=pins)

        (edit, setting) = runs(item)
        self.assertEqual(edit[0], "alpha: pin .github/workflows/ci.yml")
        # The same line is edited once, and the tag stays beside the commit.
        self.assertEqual(edit[1].count("s/^"), 1)
        self.assertIn("checkout\\@" + "a" * 40 + "\\ \\#\\ v4", edit[1])
        self.assertEqual(
            setting,
            (
                "alpha: then require pinning",
                "gh api -X PUT repos/example/alpha/actions/permissions -F enabled=true -F sha_pinning_required=true",
            ),
        )

    @skipUnless(shutil.which("perl"), "the pasted edit runs perl")
    def test_the_pasted_edit_pins_only_uses_lines_and_keeps_line_endings(self):
        sha = "a" * 40
        pins = [
            {
                "path": ".github/workflows/ci.yml",
                "uses": uses,
                "action": uses.split("@")[0],
                "ref": uses.split("@")[1],
                "sha": sha,
            }
            for uses in ("actions/checkout@v4", "example/setup@v2")
        ]
        before = (
            "jobs:\r\n"
            "  build:\r\n"
            "    steps:\r\n"
            "      - uses: actions/checkout@v4   \r\n"
            "      # - uses: actions/checkout@v4\r\n"
            "      - name: setup\r\n"
            '        uses: "example/setup@v2" # the old tag\r\n'
            "      - uses: actions/checkout@v4.1\r\n"
            "      - run: echo uses: actions/checkout@v4\r\n"
        )
        after = (
            "jobs:\r\n"
            "  build:\r\n"
            "    steps:\r\n"
            f"      - uses: actions/checkout@{sha} # v4\r\n"
            "      # - uses: actions/checkout@v4\r\n"
            "      - name: setup\r\n"
            f"        uses: example/setup@{sha} # v2\r\n"
            "      - uses: actions/checkout@v4.1\r\n"
            "      - run: echo uses: actions/checkout@v4\r\n"
        )
        item = self.item("actions-pinned", access=kept(pinning_required=False), pins=pins)
        command = runs(item)[0][1]

        with tempfile.TemporaryDirectory() as checkout:
            workflow = Path(checkout, ".github", "workflows", "ci.yml")
            workflow.parent.mkdir(parents=True)
            workflow.write_bytes(before.encode())
            subprocess.run(["/bin/sh", "-c", command], cwd=checkout, check=True)

            self.assertEqual(workflow.read_bytes().decode(), after)
            self.assertEqual(sorted(path.name for path in workflow.parent.iterdir()), ["ci.yml"])

    def test_a_workflow_called_from_another_repository_holds_back_the_setting(self):
        pins = [
            {
                "path": ".github/workflows/ci.yml",
                "uses": "actions/checkout@v4",
                "action": "actions/checkout",
                "ref": "v4",
                "sha": "a" * 40,
            }
        ]
        called = ["example/shared/.github/workflows/build.yml@" + "c" * 40]
        item = self.item("actions-pinned", access=kept(pinning_required=False), pins=pins, called_workflows=called)

        self.assertEqual(runs(item)[0][0], "alpha: pin .github/workflows/ci.yml")
        self.assertNotIn("sha_pinning_required", str(item.steps))
        # Why, in one sentence that names no workflow; which workflows, as a
        # step each.
        *each, reason = told(item)
        self.assertEqual(reason, PINS_CALLED)
        self.assertEqual(each, [f"alpha: check that {called[0]} pins its own actions, then require pinning."])

    def test_a_tag_that_could_not_be_resolved_holds_back_the_setting(self):
        pins = [
            {
                "path": ".github/workflows/ci.yml",
                "uses": "example/gone@v1",
                "action": "example/gone",
                "ref": "v1",
                "sha": "",
            }
        ]
        item = self.item("actions-pinned", access=kept(pinning_required=False), pins=pins)

        self.assertEqual(runs(item), [])
        self.assertIn("example/gone@v1", item.no_help_reason)
        self.assertNotIn("sha_pinning_required", str(item.steps))

    def test_a_wired_secret_kept_as_a_variable_moves_before_it_is_deleted(self):
        item = self.item("no-variables", variables=["HQ_APP_CLIENT_ID", "DEPLOY_TARGET"])

        self.assertEqual(
            [summary for _label, summary in runs(item)],
            [
                'gh secret set HQ_APP_CLIENT_ID -R example/alpha --body "$(gh variable get HQ_APP_CLIENT_ID -R example/alpha)"',
                "gh variable delete HQ_APP_CLIENT_ID -R example/alpha",
                "gh variable delete DEPLOY_TARGET -R example/alpha",
            ],
        )
        self.assertIn("only after its workflows read secrets.HQ_APP_CLIENT_ID", runs(item)[1][0])

    def test_a_collaborator_and_a_writable_key_are_removed_by_name(self):
        guest = self.item(
            "only-you",
            access=kept(collaborators=[{"login": "example", "role": "admin"}, {"login": "guest", "role": "write"}]),
        )
        key = self.item(
            "keys-read-only",
            access=kept(deploy_keys=[{"title": "deploy", "read_only": False, "last_used": timezone.now().isoformat()}]),
        )

        self.assertEqual(
            runs(guest), [("alpha: remove guest", "gh api -X DELETE repos/example/alpha/collaborators/guest")]
        )
        self.assertEqual(
            runs(key)[0][1],
            "gh api -X DELETE repos/example/alpha/keys/"
            "$(gh api repos/example/alpha/keys --jq '.[] | select(.title == \"deploy\") | .id')",
        )

    def test_a_public_repository_gets_the_ruleset_and_scanning_commands(self):
        store(
            private=False,
            access=kept(security={"secret_scanning": "disabled", "secret_scanning_push_protection": "enabled"}),
            variables=[],
            rules={"pull_request": True, "blocks_force_push": True, "blocks_deletion": False},
        )
        items = {finding_key(item): item for item in raised_items()}

        (deletion,) = runs(items["finding:github-posture::deletion-blocked"])
        (scanning,) = runs(items["finding:github-posture::secret-scanning"])

        self.assertTrue(deletion[1].startswith("gh api -X POST repos/example/alpha/rulesets --input - <<< '"))
        self.assertIn('"rules":[{"type":"deletion"}]', deletion[1])
        self.assertEqual(
            scanning[1],
            "gh api -X PATCH repos/example/alpha -f 'security_and_analysis[secret_scanning][status]=enabled'",
        )

    def test_every_check_has_commands_or_a_reason(self):
        for check in STANDARD:
            with self.subTest(check=check.id):
                self.assertTrue(check.id in COMMANDS or REASONS.get(check.id))


def pin(uses: str) -> dict[str, str]:
    action, _, ref = uses.rpartition("@")
    return {"path": ".github/workflows/ci.yml", "uses": uses, "action": action, "ref": ref, "sha": "a" * 40}


@override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/host")
class FollowingTheHostTests(TestCase):
    """A repository that calls HQ's own workflows at main misses pinning on purpose."""

    def state(self, name="example/alpha", *uses):
        store(
            repository=name,
            url=f"https://github.com/{name}",
            private=True,
            access=kept(pinning_required=False),
            variables=[],
            pins=[pin(line) for line in uses],
        )
        return {item.subject.name: item for item in postures()}[name].state_of("actions-pinned")

    def test_only_the_hosts_own_workflows_left_unpinned_is_intended(self):
        state = self.state(
            "example/alpha",
            "example/host/.github/workflows/plugin-checks.yml@main",
            "example/host/.github/actions/admit-plugin@main",
        )

        self.assertEqual(state, INTENDED)
        self.assertNotIn("finding:github-posture::actions-pinned", {finding_key(item) for item in raised_items()})

    def test_another_action_left_unpinned_is_still_a_gap(self):
        state = self.state(
            "example/alpha", "example/host/.github/workflows/plugin-checks.yml@main", "actions/checkout@v4"
        )

        self.assertEqual(state, Standing.UNMET)

    def test_the_host_is_held_to_pinning_itself(self):
        self.assertEqual(self.state("example/host", "example/host/.github/actions/admit-plugin@main"), Standing.UNMET)

    @override_settings(SEVERINO_HQ_SOURCE="")
    def test_a_host_that_names_no_source_excuses_nothing(self):
        self.assertEqual(self.state("example/alpha", "example/host/.github/actions/admit-plugin@main"), Standing.UNMET)
