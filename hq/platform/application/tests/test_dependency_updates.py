"""The dependency update run as HQ plans it: which repositories, by owner, and when."""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.projects.models import Project

from .. import dependency_updates
from ..capabilities import execute_capability
from ..domains import records_of
from ..projection import projection_scope
from ..security import Capability, Principal, cli_principal

KIND = dependency_updates.KIND


def opted_in(slug: str, repository_url: str, *, enabled: bool = True) -> Project:
    return Project.objects.create(name=slug, slug=slug, repository_url=repository_url, dependency_updates=enabled)


def ran(*, started, outcome="ok", reachable=True, updated=None):
    """Hold a ``github.dependency_run`` reading as a sweep would have stored it."""

    record = {
        "owner": "example",
        "repositories": ["example/app"],
        "outcome": outcome,
        "exit_code": None if outcome == "handed_off" else 0,
        "started_at": started.isoformat(),
        "finished_at": "",
    }
    row = ProviderInventory.objects.create(kind=KIND, records=[record], observed_at=started, reachable=reachable)
    if updated is not None:
        ProviderInventory.objects.filter(pk=row.pk).update(updated_at=updated)


class PlanTests(TestCase):
    def test_nothing_opted_in_is_nothing_to_run(self):
        opted_in("off", "https://github.com/example/off", enabled=False)

        self.assertEqual(dependency_updates.plan(), {"owners": [], "due": False, "last_started_at": ""})

    def test_opted_in_repositories_are_grouped_by_owner(self):
        opted_in("app", "https://github.com/Example/App")
        opted_in("tool", "https://github.com/example/tool")
        opted_in("site", "https://github.com/example-org/site")
        opted_in("off", "https://github.com/example/off", enabled=False)

        self.assertEqual(
            dependency_updates.plan()["owners"],
            [
                {"owner": "example", "repositories": ["example/app", "example/tool"]},
                {"owner": "example-org", "repositories": ["example-org/site"]},
            ],
        )

    def test_a_project_without_a_github_repository_is_not_covered(self):
        opted_in("notes", "")
        opted_in("elsewhere", "https://git.example.com/example/app")

        self.assertEqual(dependency_updates.plan()["owners"], [])

    def test_a_repository_never_run_is_due(self):
        opted_in("app", "https://github.com/example/app")

        self.assertTrue(dependency_updates.plan()["due"])

    def test_a_run_today_is_not_due_and_one_a_day_old_is(self):
        opted_in("app", "https://github.com/example/app")
        started = timezone.now() - timedelta(hours=2)
        ran(started=started)

        found = dependency_updates.plan()
        self.assertFalse(found["due"])
        self.assertEqual(found["last_started_at"], started.isoformat())
        self.assertTrue(dependency_updates.plan(started + dependency_updates.DEPENDENCY_RUN_EVERY)["due"])

    def test_a_run_handed_off_and_not_finished_is_not_handed_off_again(self):
        opted_in("app", "https://github.com/example/app")
        ran(started=timezone.now(), outcome="handed_off")

        self.assertFalse(dependency_updates.plan()["due"])

    def test_a_hand_off_that_failed_is_not_tried_on_every_sweep(self):
        opted_in("app", "https://github.com/example/app")
        now = timezone.now()
        ran(started=now - timedelta(days=2), reachable=False, updated=now)

        self.assertFalse(dependency_updates.plan()["due"])
        self.assertTrue(dependency_updates.plan(now + dependency_updates.RETRY_AFTER)["due"])

    def test_asking_makes_it_due_whatever_its_age(self):
        opted_in("app", "https://github.com/example/app")
        ran(started=timezone.now())

        result = execute_capability(dependency_updates.RUN_NOW, {}, principal=cli_principal())

        self.assertTrue(result["ok"], result)
        self.assertTrue(dependency_updates.plan()["due"])
        self.assertIsNotNone(dependency_updates.asked_at())

    def test_the_controller_registry_carries_the_plan(self):
        from ..controller import controller_registry

        opted_in("app", "https://github.com/example/app")

        self.assertEqual(controller_registry()["dependency_updates"], dependency_updates.plan())


class RunNowTests(TestCase):
    def setUp(self):
        opted_in("app", "https://github.com/example/app")

    def run_now(self, payload: dict, *, principal: Principal | None = None) -> dict:
        return execute_capability(dependency_updates.RUN_NOW, payload, principal=principal or cli_principal())

    def test_a_principal_without_project_writes_is_denied(self):
        reader = Principal("example-reader", "mcp", frozenset({Capability.READ}))

        self.assertEqual(self.run_now({}, principal=reader)["error"]["code"], "forbidden")
        self.assertIsNone(dependency_updates.asked_at())

    def test_a_project_writer_who_may_not_ask_the_controller_is_denied(self):
        writer = Principal("example-writer", "mcp", frozenset({Capability.READ, records_of("projects").write}))

        self.assertEqual(self.run_now({}, principal=writer)["error"]["code"], "forbidden")
        self.assertIsNone(dependency_updates.asked_at())

    def test_an_unknown_field_is_refused(self):
        self.assertEqual(self.run_now({"owner": "example"})["error"]["code"], "invalid_input")
        self.assertIsNone(dependency_updates.asked_at())

    def test_nothing_opted_in_is_refused(self):
        Project.objects.update(dependency_updates=False)

        self.assertEqual(self.run_now({})["error"]["code"], "precondition")
        self.assertIsNone(dependency_updates.asked_at())


class ReadingTests(TestCase):
    """The reading keeps what root said of a run, and nothing a run could leak."""

    def spec(self):
        return OBSERVATIONS[KIND]

    def test_a_record_keeps_only_the_schemas_fields(self):
        record = {
            "owner": "example",
            "repositories": ["example/app"],
            "outcome": "failed",
            "exit_code": 1,
            "started_at": "2030-01-02T03:00:00Z",
            "finished_at": "2030-01-02T03:20:00Z",
            "token": "example-token",
            "log": "example log line",
        }

        kept, refused = self.spec().clean([record])

        self.assertFalse(refused)
        self.assertNotIn("token", kept[0])
        self.assertNotIn("log", kept[0])
        self.assertEqual(kept[0]["exit_code"], 1)

    def test_a_record_with_an_unknown_outcome_or_repository_is_refused(self):
        good = {"owner": "example", "repositories": ["example/app"], "outcome": "ok", "exit_code": 0}
        for bad in (
            {**good, "outcome": "great"},
            {**good, "repositories": ["example/app; rm"]},
            {**good, "owner": "Example Org"},
            {key: value for key, value in good.items() if key != "exit_code"},
        ):
            kept, refused = self.spec().clean([bad])
            self.assertEqual((kept, bool(refused)), ([], True), bad)


def reading(*records: dict) -> None:
    ProviderInventory.objects.create(kind=KIND, records=list(records), observed_at=timezone.now())


def run_of(owner: str, repositories: list[str], started, *, outcome: str = "ok") -> dict:
    return {
        "owner": owner,
        "repositories": repositories,
        "outcome": outcome,
        "exit_code": 0,
        "started_at": started.isoformat(),
        "finished_at": (started + timedelta(minutes=5)).isoformat(),
    }


class LastRunTests(TestCase):
    """A repository's latest run, from the stored reading alone."""

    def test_a_repository_no_run_named_has_none(self):
        self.assertIsNone(dependency_updates.last_run("https://github.com/example/app"))
        reading(run_of("example", ["example/tool"], timezone.now()))

        self.assertIsNone(dependency_updates.last_run("https://github.com/example/app"))

    def test_a_run_that_named_it_is_its_last_run(self):
        started = timezone.now() - timedelta(hours=3)
        reading(run_of("example", ["example/app", "example/tool"], started, outcome="failed"))

        run = dependency_updates.last_run("https://github.com/example/app")

        self.assertEqual((run.outcome, run.started_at), ("failed", started))
        self.assertEqual(run.as_record()["finished_at"], (started + timedelta(minutes=5)).isoformat())

    def test_the_latest_run_that_named_it_wins_and_another_owners_is_its_own(self):
        now = timezone.now()
        reading(
            run_of("example", ["example/app"], now - timedelta(hours=1), outcome="handed_off"),
            run_of("example", ["example/app"], now - timedelta(days=1), outcome="failed"),
            run_of("example-org", ["example-org/site"], now - timedelta(days=3)),
        )

        self.assertEqual(dependency_updates.last_run("example/app").outcome, "handed_off")
        self.assertEqual(dependency_updates.last_run("https://github.com/example-org/site").outcome, "ok")

    def test_a_repository_is_matched_whatever_its_case(self):
        reading(run_of("example", ["example/app"], timezone.now()))

        self.assertIsNotNone(dependency_updates.last_run("https://github.com/Example/App"))

    def test_a_url_that_names_no_repository_has_none(self):
        reading(run_of("example", ["example/app"], timezone.now()))

        self.assertIsNone(dependency_updates.last_run(""))
        self.assertIsNone(dependency_updates.last_run("https://git.example.com/example/app"))

    def test_the_reading_is_read_once_per_projection(self):
        reading(run_of("example", ["example/app", "example/tool"], timezone.now()))

        with projection_scope(), CaptureQueriesContext(connection) as once:
            dependency_updates.last_run("example/app")
        with projection_scope(), CaptureQueriesContext(connection) as twice:
            dependency_updates.last_run("example/app")
            dependency_updates.last_run("example/tool")

        self.assertEqual(len(twice), len(once))
