"""A project's Refresh asks the GitHub App for a fresh read of its repository.

Where the App reads the repository, Refresh makes the same request the
Connections page's Read now makes, through the same capability, so the pull
requests, checks and workflows on the project page follow on the controller's
next pass. Where it does not, the anonymous public read is the fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
from hq.platform.core.models import AuditLog
from hq.domains.projects.models import Project

from ..projects import refresh_project
from ..security import OPERATOR_CAPABILITIES, Capability, Principal

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

    def __init__(self, pushed_at=datetime(2026, 7, 31, 20, 0, tzinfo=dt_timezone.utc)):
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
        self.assertEqual(
            list(ReadRequest.objects.values_list("connection_ref", "kind")), [(REF, "")]
        )
        # The App's own read is the source, so the public one is not asked.
        self.assertEqual(fetcher.asked, [])
        self.project.refresh_from_db()
        self.assertEqual(self.project.last_push_at, datetime(2026, 9, 27, 12, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(result["github"], {"ok": True, "last_push_at": "2026-09-27T12:00:00+00:00"})

    def test_a_principal_that_may_not_wake_the_controller_falls_back_to_the_public_read(self):
        read_by_app()
        writer = Principal("agent", "mcp", frozenset({Capability.READ, "write_projects"}))

        result, fetcher = self.refresh(writer)

        self.assertFalse(result["github_app"]["ok"])
        self.assertIn("manage_infrastructure", result["github_app"]["error"])
        self.assertFalse(ReadRequest.objects.exists())
        # Refused where every capability is refused, and recorded there.
        self.assertTrue(
            AuditLog.objects.filter(metadata__capability="infrastructure.controller.refresh").exists()
        )
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

    def test_the_button_asks_the_app_and_says_so(self):
        read_by_app()

        response = self.client.post(reverse("projects:refresh", args=["alpha"]), follow=True)

        self.assertEqual(list(ReadRequest.objects.values_list("connection_ref", flat=True)), [REF])
        shown = [str(message) for message in response.context["messages"]]
        self.assertTrue(any(REF in message for message in shown), shown)
