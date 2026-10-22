"""Opting a project in to dependency updates: one audited switch, held to its precondition."""

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.projects.models import Project
from hq.domains.projects.views import run_now_ask
from hq.platform.application.action_links import command_url
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.dependency_updates import KIND as RUN_KIND, RUN_NOW, asked_at
from hq.platform.application.domains import records_of
from hq.platform.application.projects import DEPENDENCY_UPDATES, get_project, list_projects
from hq.platform.application.security import Capability, Principal, cli_principal
from hq.platform.application.tests import test_github_estate as estate
from hq.platform.application.tests.markup import Element, all_elements, hrefs, select, text, with_class, within
from hq.platform.application.tests.test_github_estate import store
from hq.platform.application.tests.test_github_posture import kept
from hq.platform.core.models import AuditLog

URL = "https://github.com/example/alpha"


def switch(slug: str, payload: dict, *, principal: Principal | None = None) -> dict:
    return execute_capability(DEPENDENCY_UPDATES, payload, principal=principal or cli_principal(), target=slug)


class DependencyUpdatesTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Alpha", slug="alpha", repository_url=URL)

    def test_a_repository_with_alerts_on_is_opted_in_and_the_switch_is_audited(self):
        store(access=kept())
        edits = AuditLog.objects.filter(object_id=str(self.project.pk), action=AuditLog.Action.UPDATED)
        before = edits.count()

        result = switch("alpha", {"enabled": True})

        self.assertTrue(result["ok"], result)
        self.assertIs(result["project"]["updates"]["enabled"], True)
        self.project.refresh_from_db()
        self.assertTrue(self.project.dependency_updates)
        self.assertGreater(edits.count(), before)

        self.assertTrue(switch("alpha", {"enabled": False})["ok"])
        self.project.refresh_from_db()
        self.assertFalse(self.project.dependency_updates)

    def test_alerts_off_refuses_and_names_the_check(self):
        store(access=kept(vulnerability_alerts=False))

        result = switch("alpha", {"enabled": True})

        self.assertEqual(result["error"]["code"], "precondition")
        self.assertIn("vulnerability-alerts", result["error"]["message"])
        self.project.refresh_from_db()
        self.assertFalse(self.project.dependency_updates)

    def test_a_repository_the_app_does_not_read_cannot_be_opted_in(self):
        result = switch("alpha", {"enabled": True})

        self.assertEqual(result["error"]["code"], "precondition")

    def test_a_project_with_no_repository_is_refused(self):
        Project.objects.create(name="Notes", slug="notes")

        for enabled in (True, False):
            self.assertEqual(switch("notes", {"enabled": enabled})["error"]["code"], "precondition")

    def test_a_principal_without_project_writes_is_denied(self):
        store(access=kept())
        reader = Principal("example-reader", "mcp", frozenset({Capability.READ}))

        result = switch("alpha", {"enabled": True}, principal=reader)

        self.assertEqual(result["error"]["code"], "forbidden")
        self.project.refresh_from_db()
        self.assertFalse(self.project.dependency_updates)

    def test_an_unknown_project_or_field_is_refused(self):
        self.assertEqual(switch("example-missing", {"enabled": True})["error"]["code"], "not_found")
        extra = switch("alpha", {"enabled": True, "repository_url": URL})
        self.assertEqual(extra["error"]["code"], "invalid_input")
        self.assertEqual(switch("alpha", {})["error"]["code"], "invalid_input")

    def test_a_stale_read_is_a_conflict(self):
        store(access=kept())

        result = execute_capability(
            DEPENDENCY_UPDATES,
            {"enabled": True},
            principal=cli_principal(),
            target="alpha",
            expected_updated_at="2000-01-01T00:00:00+00:00",
        )

        self.assertEqual(result["error"]["code"], "conflict")

    def test_the_generic_project_update_cannot_set_the_switch(self):
        result = execute_capability(
            "project.update", {"name": "Alpha", "dependency_updates": True}, principal=cli_principal(), target="alpha"
        )

        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_the_project_page_links_the_command(self):
        self.client.force_login(get_user_model().objects.create_user("project-reader"))

        response = self.client.get(reverse("projects:detail", args=["alpha"]))

        self.assertIn(command_url(DEPENDENCY_UPDATES, "alpha"), hrefs(response))


# What the reading holds for a repository with updates open: a long update
# title as Renovate writes one, a person's pull request, an alert the update
# answers and one it does not.
LONG_TITLE = "Update dependency example-lib to v2.14.3 [SECURITY] (example/alpha web workspace)"
UPDATE = {**estate.UPDATE, "title": LONG_TITLE}
OPEN = {
    "pull_requests": [UPDATE, estate.BUMP],
    "dependabot_alerts": [estate.ANSWERED, estate.UNANSWERED],
}


class UpdatesSurfaceTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("updates-reader"))
        self.project = Project.objects.create(
            name="Alpha", slug="alpha", repository_url=URL, dependency_updates=True, status=Project.Status.ACTIVE
        )

    def test_the_project_page_lists_updates_and_the_alerts_none_answers(self):
        store(**OPEN)

        response = self.client.get(reverse("projects:detail", args=["alpha"]))

        updates = [element.attrs["href"] for element in within(response, "data-updates") if element.tag == "a"]
        alerts = [element.attrs["href"] for element in within(response, "data-unmatched-alerts") if element.tag == "a"]
        self.assertEqual(updates, [UPDATE["url"]])
        self.assertEqual(alerts, [estate.UNANSWERED["url"]])
        self.assertIn(LONG_TITLE, text(response))
        self.assertIn("Checks failing", text(response))
        self.assertIn("no fixed version yet", text(response))
        # A person's pull request is listed with the others, never as an update.
        self.assertIn(estate.BUMP["url"], hrefs(response))
        self.assertEqual(len(select(response, "id", "updates")), 1)
        (state,) = select(response, "data-updates-state")
        self.assertIn("1 open update", state.words)
        self.assertIn("#updates", [element.attrs.get("href") for element in within(response, "data-updates-state")])

    def test_a_project_not_opted_in_says_so_and_draws_no_empty_lists(self):
        Project.objects.filter(pk=self.project.pk).update(dependency_updates=False)
        store()

        response = self.client.get(reverse("projects:detail", args=["alpha"]))

        (state,) = select(response, "data-updates-state")
        self.assertIn("Off", state.words)
        self.assertEqual(select(response, "id", "updates"), [])

    def test_the_list_says_whether_updates_are_on_and_filters_on_it(self):
        store(**OPEN, alerts={"dependabot": {"critical": 1, "high": 1}})
        Project.objects.create(name="Beta", slug="beta", repository_url="https://github.com/example/beta")
        Project.objects.create(name="Notes", slug="notes")

        response = self.client.get(reverse("projects:list"))
        filtered = self.client.get(reverse("projects:list"), {"updates_on": "1"})

        self.assertIn("/projects/alpha/#updates", hrefs(response))
        self.assertIn("1 open update", text(response))
        # The alert count is a figure, so its state is its ink, never a chip.
        self.assertIn("2 open alerts", [figure.words for figure in with_class(response, "tone-attention")])
        self.assertEqual({project.slug for project in filtered.context["object_list"]}, {"alpha"})

    def test_the_record_carries_the_update_state_when_it_names_a_repository(self):
        store(**OPEN)
        Project.objects.create(name="Notes", slug="notes")

        with mock.patch("hq.platform.application.derivations._clock", return_value=datetime(2030, 1, 10, tzinfo=UTC)):
            alpha = get_project("alpha")
        notes = get_project("notes")

        self.assertEqual(
            alpha["updates"],
            {"enabled": True, "open_pulls": 1, "oldest_pull_days": 7, "unmatched_alerts": 1, "last_run": None},
        )
        self.assertNotIn("updates", notes)

    def test_a_repository_the_app_does_not_read_or_whose_alerts_were_refused_counts_nothing(self):
        Project.objects.create(name="Beta", slug="beta", repository_url="https://github.com/example/beta")
        store(pull_requests=[UPDATE], dependabot_alerts=None)

        self.assertEqual(get_project("beta")["updates"]["open_pulls"], None)
        self.assertEqual(get_project("alpha")["updates"]["unmatched_alerts"], None)
        self.assertEqual(get_project("alpha")["updates"]["open_pulls"], 1)

    def test_the_list_resource_filters_on_updates(self):
        store()
        Project.objects.create(name="Beta", slug="beta", repository_url="https://github.com/example/beta")

        on = list_projects(updates=True)["items"]
        off = list_projects(updates=False)["items"]

        self.assertEqual(([item["slug"] for item in on], [item["slug"] for item in off]), (["alpha"], ["beta"]))


def ran(outcome: str, *, hours_ago: int = 2) -> datetime:
    """Hold a ``github.dependency_run`` reading whose one run covered alpha."""

    started = timezone.now() - timedelta(hours=hours_ago)
    record = {
        "owner": "example",
        "repositories": ["example/alpha"],
        "outcome": outcome,
        "exit_code": None if outcome == "handed_off" else 0,
        "started_at": started.isoformat(),
        "finished_at": "" if outcome == "handed_off" else (started + timedelta(minutes=4)).isoformat(),
    }
    ProviderInventory.objects.update_or_create(
        kind=RUN_KIND, defaults={"records": [record], "observed_at": started, "reachable": True}
    )
    return started


def run_controls(response) -> list[Element]:
    """Each ask on the page whose button posts a run."""

    url = reverse("projects:updates_run", args=["alpha"])
    return [
        next(ancestor for ancestor in button.ancestors if "data-ask" in ancestor.attrs)
        for button in all_elements(response)
        if button.attrs.get("formaction") == url
    ]


class LastRunSurfaceTests(TestCase):
    """The project page and record say when the last run was and how it ended."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("runs-reader"))
        self.project = Project.objects.create(name="Alpha", slug="alpha", repository_url=URL, dependency_updates=True)
        store()

    def state(self):
        response = self.client.get(reverse("projects:detail", args=["alpha"]))
        (state,) = select(response, "data-updates-state")
        return response, state

    def test_a_repository_never_run_says_so(self):
        _, state = self.state()

        self.assertIn("Not run yet", state.words)

    def test_the_last_run_shows_its_outcome(self):
        for outcome, said in (
            ("ok", "Last ran"),
            ("failed", "Failed"),
            ("timeout", "Stopped early"),
            ("handed_off", "Running"),
        ):
            with self.subTest(outcome):
                ran(outcome)
                response, state = self.state()

                (run,) = select(response, "data-updates-run")
                self.assertEqual(run.attrs["data-updates-run"], outcome)
                self.assertIn(said, run.words)
                self.assertIn("2 hours ago", state.words)

    def test_a_run_asked_for_and_not_yet_handed_off_says_so_and_is_followed(self):
        ran("ok", hours_ago=30)
        self.assertTrue(execute_capability(RUN_NOW, {}, principal=cli_principal())["ok"])

        response, _ = self.state()

        self.assertEqual(len(select(response, "data-run-requested")), 1)
        asks = run_controls(response)
        self.assertTrue(asks)
        for ask in asks:
            self.assertTrue(ask.attrs.get("data-ask-status"))
            self.assertEqual(ask.attrs["data-ask-refresh"], "#updates-state")

    def test_the_control_posts_the_run_and_the_page_follows_it(self):
        response, _ = self.state()
        self.assertTrue(run_controls(response))

        posted = self.client.post(
            reverse("projects:updates_run", args=["alpha"]), HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        self.assertEqual(posted.status_code, 202)
        self.assertTrue(posted.json()["status"])
        self.assertIsNotNone(asked_at())

    def test_a_project_not_opted_in_offers_no_run(self):
        Project.objects.filter(pk=self.project.pk).update(dependency_updates=False)

        response, state = self.state()

        self.assertEqual(run_controls(response), [])
        self.assertNotIn("Not run yet", state.words)

    def test_the_control_is_drawn_only_for_a_principal_the_capability_permits(self):
        writer = Principal("example-writer", "web", frozenset({Capability.READ, records_of("projects").write}))

        self.assertIsNotNone(run_now_ask(self.project, cli_principal(), None))
        self.assertIsNone(run_now_ask(self.project, writer, None))

    def test_the_record_carries_the_last_run(self):
        self.assertIsNone(get_project("alpha")["updates"]["last_run"])
        started = ran("failed")

        self.assertEqual(
            get_project("alpha")["updates"]["last_run"],
            {
                "outcome": "failed",
                "started_at": started.isoformat(),
                "finished_at": (started + timedelta(minutes=4)).isoformat(),
            },
        )
