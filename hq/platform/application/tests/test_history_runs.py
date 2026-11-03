"""A burst of like moments from one source, in one place, reads as one event."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.application.tests.markup import select, text
from hq.platform.core import append_only
from hq.platform.core.models import AuditLog

# Written out, so the module imports at a ref without the names.
SHOWN = 5


class MomentRunTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))
        with append_only.lifted():
            AuditLog.objects.all().delete()

    def started(self, *starts):
        ProviderInventory.objects.create(
            kind="portainer.runtime",
            observed_at=self.now,
            records=[
                {
                    "container": name,
                    "host": host,
                    "connection_ref": "example-portainer",
                    "started_at": (self.now - ago).isoformat(),
                }
                for name, host, ago in starts
            ],
        )

    def page(self):
        response = self.client.get(reverse("core:audit_list"))
        return list(response.context["object_list"]), response.content.decode()

    def test_a_hosts_containers_started_together_are_one_event_with_them_folded_beneath(self):
        # A host's boot: nine starts over three minutes.
        self.started(*((f"app-{index}", "example-box", timedelta(hours=1, seconds=index * 20)) for index in range(9)))

        rows, html = self.page()
        heads = [row for row in rows if not row.under]

        self.assertEqual(len(heads), 1)
        self.assertIn("9 containers started on example-box", text(html))
        # The newest beneath it, the rest counted, under the head's own fold.
        (head,) = select(html, "data-folds")
        self.assertEqual(len(select(html, "data-folded", head.attrs["id"])), SHOWN + 1)
        self.assertIn("4 more containers", text(html))

    def test_starts_on_another_host_or_long_apart_stay_apart(self):
        self.started(
            ("app", "example-box", timedelta(hours=1)),
            ("db", "example-box", timedelta(hours=1, minutes=1)),
            ("app", "example-other", timedelta(hours=1)),
            ("db", "example-box", timedelta(hours=3)),
        )

        rows, html = self.page()
        heads = [row for row in rows if not row.under]

        self.assertEqual(len(heads), 3)
        self.assertIn("2 containers started on example-box", text(html))
        self.assertIn("app started on example-other", text(html))

    def test_a_run_reaches_no_further_back_than_its_window_from_its_newest(self):
        # One start every ten minutes for an hour and a half: no chain of
        # close starts makes the whole stretch one event.
        self.started(*((f"app-{index}", "example-box", timedelta(minutes=10 * index)) for index in range(10)))

        rows, _html = self.page()
        heads = [row for row in rows if not row.under]

        self.assertGreater(len(heads), 1)
        self.assertLess(len(heads), 10)
