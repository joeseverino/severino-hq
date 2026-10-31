"""A project's Refresh asks the GitHub App for a fresh read of its repository.

Where the App reads the repository, Refresh makes the same request the
Connections page's Refresh makes, through the same capability, so the pull
requests, checks and workflows on the project page follow on the controller's
next pass. Where it does not, the anonymous public read is the fallback.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
from hq.domains.jobs.models import HEARTBEAT_GRACE, Job
from hq.domains.jobs.testing import held_jobs
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog

from ..capabilities import execute_capability
from ..outbound_work import ANOTHER_RUNNING
from ..projects import REFRESH, refresh_project
from ..security import OPERATOR_CAPABILITIES, Capability, Principal
from .markup import select, within

DIRECTORY = Path(tempfile.mkdtemp())
MARKERS = override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
)
REF = "example-github"
PUSHED = "2026-09-27T12:00:00Z"
OPERATOR = Principal("operator", "web", OPERATOR_CAPABILITIES)


def read_by_app(repository: str = "example/alpha") -> None:
    ProviderConnection.objects.update_or_create(
        controller_id="example-controller",
        connection_ref=REF,
        defaults={"provider": "github_app", "observed_at": timezone.now()},
    )
    ProviderInventory.objects.update_or_create(
        kind="github.repository",
        defaults={
            "records": [{"connection_ref": REF, "repository": repository, "pushed_at": PUSHED}],
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now(),
        },
    )


class Fetcher:
    """The anonymous public read, counted."""

    def __init__(self, pushed_at=datetime(2026, 7, 31, 20, 0, tzinfo=UTC)):
        self.asked: list[str] = []
        self.pushed_at = pushed_at

    def __call__(self, repository_url, **kwargs):
        self.asked.append(repository_url)
        return self.pushed_at


@MARKERS
class RefreshAsksTheAppTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha"
        )

    def refresh(self, principal=OPERATOR):
        fetcher = Fetcher()
        result = refresh_project(self.project.slug, principal=principal, github_fetcher=fetcher)
        return result, fetcher

    def test_a_repository_the_app_reads_is_read_now_through_the_connection(self):
        read_by_app()

        result, fetcher = self.refresh()

        self.assertTrue(result["github_app"]["ok"], result)
        self.assertEqual(result["github_app"]["connection_ref"], REF)
        self.assertIn(REF, result["github_app"]["message"])
        self.assertEqual(list(ReadRequest.objects.values_list("connection_ref", "kind")), [(REF, "")])
        # The App's own read is the source, so the public one is not asked.
        self.assertEqual(fetcher.asked, [])
        self.project.refresh_from_db()
        self.assertEqual(self.project.last_push_at, datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.assertEqual(result["github"], {"ok": True, "last_push_at": "2026-09-27T12:00:00+00:00"})

    def test_a_principal_that_may_not_wake_the_controller_falls_back_to_the_public_read(self):
        read_by_app()
        writer = Principal("agent", "mcp", frozenset({Capability.READ, "write_projects"}))

        result, fetcher = self.refresh(writer)

        self.assertFalse(result["github_app"]["ok"])
        self.assertIn("manage_infrastructure", result["github_app"]["error"])
        self.assertFalse(ReadRequest.objects.exists())
        # Refused where every capability is refused, and recorded there.
        self.assertTrue(AuditLog.objects.filter(metadata__capability="infrastructure.controller.refresh").exists())
        self.assertEqual(fetcher.asked, [self.project.repository_url])
        self.assertTrue(result["github"]["ok"])

    def test_a_principal_that_may_not_write_projects_is_refused_before_anything(self):
        read_by_app()
        reader = Principal("agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))

        from ..security import AuthorizationError

        with self.assertRaises(AuthorizationError):
            self.refresh(reader)
        self.assertFalse(ReadRequest.objects.exists())

    def test_a_repository_the_app_does_not_read_uses_the_public_read(self):
        read_by_app("example/other")

        result, fetcher = self.refresh()

        self.assertIsNone(result["github_app"])
        self.assertFalse(ReadRequest.objects.exists())
        self.assertEqual(fetcher.asked, [self.project.repository_url])
        self.assertTrue(result["github"]["ok"])

    def test_with_no_app_reading_at_all_the_public_read_is_unchanged(self):
        result, fetcher = self.refresh()

        self.assertIsNone(result["github_app"])
        self.assertEqual(fetcher.asked, [self.project.repository_url])
        self.project.refresh_from_db()
        self.assertEqual(self.project.last_push_at, fetcher.pushed_at)

    def test_an_app_record_without_a_push_time_leaves_the_last_one(self):
        read_by_app()
        ProviderInventory.objects.filter(kind="github.repository").update(
            records=[{"connection_ref": REF, "repository": "example/alpha"}]
        )

        result, fetcher = self.refresh()

        self.assertTrue(result["github_app"]["ok"])
        self.assertEqual(fetcher.asked, [])
        self.assertEqual(result["github"], {"ok": True, "last_push_at": None})


@MARKERS
class RefreshButtonTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user(username="operator", password="x"))
        Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")
        Project.objects.create(name="Beta", slug="beta", repository_url="https://github.com/example/beta")

    def page(self, slug="alpha"):
        return self.client.get(reverse("projects:detail", args=[slug]))

    def press(self, page, **headers):
        """Post the page's Refresh control as the browser would, with the
        page's own form."""

        (button,) = select(page, "formaction", reverse("jobs:ask", args=[REFRESH]))
        (back,) = (field.attrs["value"] for field in within(page, "id", "hq-post") if field.attrs.get("name") == "next")
        data = {button.attrs["name"]: button.attrs["value"], "next": back}
        return self.client.post(button.attrs["formaction"], data, headers=headers, follow=True)

    def test_the_button_starts_a_job_and_answers_before_any_of_it_runs(self):
        read_by_app()

        with held_jobs() as jobs:
            response = self.press(self.page(), x_requested_with="XMLHttpRequest")
            answer = response.json()
            # Answered with the work still to do: nothing was asked of anyone.
            self.assertEqual((response.status_code, answer["state"], answer["live"]), (202, "queued", True))
            self.assertFalse(ReadRequest.objects.exists())
            self.assertContains(self.page(), f'data-ask-status="{answer["status"]}"')
            jobs.run()

        self.assertEqual(list(ReadRequest.objects.values_list("connection_ref", flat=True)), [REF])
        ended = self.client.get(answer["status"]).json()
        self.assertEqual((ended["state"], ended["live"]), ("done", False))
        # The job's last note is what the refresh did.
        self.assertIn(REF, ended["note"])
        job = Job.objects.get()
        self.assertEqual((job.kind, job.requested_by.username, job.actor), (REFRESH, "operator", "operator"))

    def test_without_script_the_button_returns_to_the_page_it_was_pressed_on(self):
        with held_jobs():
            response = self.press(self.page())

        self.assertRedirects(response, reverse("projects:detail", args=["alpha"]))
        self.assertTrue(response.context["messages"])
        self.assertTrue(select(response, "aria-disabled", "true"))

    def test_a_refresh_of_another_project_while_one_runs_is_refused_and_says_why(self):
        with held_jobs():
            self.press(self.page("alpha"))
            again = self.press(self.page("beta"), x_requested_with="XMLHttpRequest")

        self.assertEqual((again.status_code, again.json()["state"]), (200, "failed"))
        self.assertEqual(again.json()["note"], ANOTHER_RUNNING)
        self.assertEqual(Job.objects.count(), 1)

    def test_a_refresh_whose_process_stopped_shows_failed_and_the_page_writes_nothing(self):
        stale = timezone.now() - HEARTBEAT_GRACE - timedelta(minutes=1)
        job = Job.objects.create(
            kind=REFRESH,
            label="Refresh",
            state=Job.State.RUNNING,
            request={"subject": "alpha"},
            started_at=stale,
            heartbeat_at=stale,
        )
        before = AuditLog.objects.count()

        page = self.page()

        self.assertTrue(select(page, "data-ask-state", "failed"))
        job.refresh_from_db()
        self.assertEqual((job.state, AuditLog.objects.count()), (Job.State.RUNNING, before))

    def test_pressing_again_while_its_own_refresh_runs_follows_that_one(self):
        with held_jobs():
            first = self.press(self.page(), x_requested_with="XMLHttpRequest").json()
            again = self.press(self.page(), x_requested_with="XMLHttpRequest").json()

        self.assertEqual((again["state"], again["status"]), ("queued", first["status"]))
        self.assertEqual(Job.objects.count(), 1)


@MARKERS
class RefreshCapabilityTests(TestCase):
    """``project.refresh`` over the API, MCP and a command line is the same ask."""

    def setUp(self):
        Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")

    def test_it_starts_the_job_and_answers_at_once(self):
        with held_jobs():
            answer = execute_capability(REFRESH, {}, principal=OPERATOR, target="alpha")

        self.assertEqual((answer["ok"], answer["started"]), (True, True))
        self.assertEqual(str(Job.objects.get(kind=REFRESH).pk), answer["job"])

    def test_a_principal_that_may_not_write_projects_is_refused_and_nothing_is_recorded(self):
        reader = Principal("agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))

        with held_jobs():
            answer = execute_capability(REFRESH, {}, principal=reader, target="alpha")

        self.assertEqual(answer["error"]["code"], "forbidden")
        self.assertFalse(Job.objects.exists())

    def test_an_unknown_project_is_not_found_before_any_job(self):
        with held_jobs():
            answer = execute_capability(REFRESH, {}, principal=OPERATOR, target="missing")

        self.assertEqual(answer["error"]["code"], "not_found")
        self.assertFalse(Job.objects.exists())

    def test_it_takes_nothing_but_its_project(self):
        answer = execute_capability(REFRESH, {"slug": "alpha"}, principal=OPERATOR, target="alpha")

        self.assertEqual(answer["error"]["code"], "invalid_input")
        self.assertFalse(Job.objects.exists())
