"""An audit event stays light however much it changed.

The log's page and ``recent_activity`` carry a bounded few of an event's
items, count the rest, and point to the event's own page, which lists them all.
"""

import re
from contextlib import contextmanager
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import post_init
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.read_models import recent_activity
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.markup import select, text, within
from hq.platform.core import append_only
from hq.platform.core.models import AuditLog

OPERATION = "example-import"


def _items(count: int, *, start: int = 0) -> None:
    """``count`` more rows of one operation, written as an import writes them, oldest first."""

    now = timezone.now()
    AuditLog.objects.bulk_create(
        AuditLog(
            action=AuditLog.Action.CREATED,
            object_type="example.record",
            object_repr=f"record-{index}",
            operation_id=OPERATION,
            metadata={"operation": "example.import"},
            created_at=now - timedelta(hours=1) + timedelta(seconds=index),
        )
        for index in range(start, start + count)
    )


@contextmanager
def _read():
    """The audit rows the code inside reads, each one loaded from the database."""

    loaded = []

    def note(sender, instance, **kwargs):
        loaded.append(instance)

    post_init.connect(note, sender=AuditLog)
    try:
        yield loaded
    finally:
        post_init.disconnect(note, sender=AuditLog)


class BoundedEventTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14))
        with append_only.lifted():
            AuditLog.objects.all().delete()

    def _cost(self, draw):
        with CaptureQueriesContext(connection) as queries, _read() as loaded:
            produced = draw()
        return produced, (len(queries), len(loaded))

    def test_an_operation_of_500_costs_what_one_of_5_does(self):
        def page():
            return self.client.get(reverse("core:audit_list")).content.decode()

        def activity():
            return recent_activity(principal=cli_principal())

        _items(5)
        page(), activity()
        small_page, page_cost = self._cost(page)
        _, activity_cost = self._cost(activity)
        _items(495, start=5)
        large_page, large_page_cost = self._cost(page)
        large_activity, large_activity_cost = self._cost(activity)

        self.assertEqual(large_page_cost, page_cost, "queries and rows read, for the page")
        self.assertEqual(large_activity_cost, activity_cost, "queries and rows read, for recent_activity")
        # What it draws stays the size of the smaller event, give or take the line counting the rest.
        self.assertLess(len(large_page), len(small_page) * 1.1)
        # And it is the larger event that was drawn.
        self.assertIn("500 example records", text(large_page))
        self.assertEqual(large_activity["items"][0]["more"] + len(large_activity["items"][0]["items"]), 500)

    def test_the_page_counts_what_its_fold_leaves_out_and_links_to_all_of_it(self):
        _items(12)

        html = self.client.get(reverse("core:audit_list")).content.decode()

        (head,) = select(html, "data-folds")
        folded = select(html, "data-folded", head.attrs["id"])
        newest = AuditLog.objects.order_by("-created_at", "-pk").first()
        # The fold's last row counts the items it leaves out, linking to the page that lists them.
        *_shown, rest = [element for element in within(html, "data-folded", head.attrs["id"]) if element.tag == "a"]
        self.assertLess(len(folded), 12)
        self.assertEqual(rest.words, f"{12 - (len(folded) - 1)} more changes")
        self.assertEqual(rest.attrs["href"], f"{reverse('core:audit_detail', args=[newest.pk])}#rest-of-operation")

    def test_recent_activity_says_how_many_more_and_where_they_are(self):
        _items(12)

        (event,) = recent_activity(principal=cli_principal())["items"]

        self.assertLess(len(event["items"]), 12)
        self.assertEqual(event["more"], 12 - len(event["items"]))
        listed = self.client.get(event["url"])
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.context["event"].operation_id, OPERATION)

    def test_an_events_page_lists_every_item_of_its_operation_a_page_at_a_time(self):
        _items(120)
        newest = AuditLog.objects.order_by("-created_at", "-pk").first()
        url = reverse("core:audit_detail", args=[newest.pk])

        listed: set[str] = set()
        # Each page links the next, until the last; bounded, so a page that links itself cannot loop.
        for number in range(1, 10):
            html = self.client.get(url, {"page": number}).content.decode()
            listed |= set(re.findall(r"\brecord-\d+\b", html))
            if f"?page={number + 1}" not in html:
                break

        self.assertEqual(listed, {f"record-{index}" for index in range(120)})
