"""An audit event stays light however much it changed.

The log's page and its list of events carry a bounded few of an event's
items, count the rest, and point to the log of every row of its operation.
"""

import re
from contextlib import contextmanager
from datetime import timedelta

from django.db import connection
from django.db.models.signals import post_init
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.platform.application.markup_testing import select, text, within
from hq.platform.application.resources import list_resource
from hq.platform.application.security import cli_principal
from hq.platform.core import append_only
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator

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
        operator(self.client)
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
            return list_resource("audit", {"events": True}, principal=cli_principal())

        _items(5)
        page(), activity()
        small_page, page_cost = self._cost(page)
        _, activity_cost = self._cost(activity)
        _items(495, start=5)
        large_page, large_page_cost = self._cost(page)
        large_activity, large_activity_cost = self._cost(activity)

        self.assertEqual(large_page_cost, page_cost, "queries and rows read, for the page")
        self.assertEqual(large_activity_cost, activity_cost, "queries and rows read, for the list of events")
        # What it draws stays the size of the smaller event, give or take the line counting the rest.
        self.assertLess(len(large_page), len(small_page) * 1.1)
        # And it is the larger event that was drawn.
        self.assertIn("500 example records", text(large_page))
        self.assertEqual(large_activity["items"][0]["more"] + len(large_activity["items"][0]["changed"]), 500)

    def test_the_page_counts_what_its_fold_leaves_out_and_links_to_all_of_it(self):
        _items(12)

        html = self.client.get(reverse("core:audit_list")).content.decode()

        (head,) = select(html, "data-folds")
        folded = select(html, "data-folded", head.attrs["id"])
        # The fold's last row counts the items it leaves out, linking to the log that lists them.
        *_shown, rest = [element for element in within(html, "data-folded", head.attrs["id"]) if element.tag == "a"]
        self.assertLess(len(folded), 12)
        self.assertEqual(rest.words, f"{12 - (len(folded) - 1)} more changes")
        self.assertEqual(rest.attrs["href"], f"{reverse('core:audit_list')}?chain={OPERATION}")

    def test_an_event_says_how_many_more_and_where_they_are(self):
        _items(12)

        (event,) = list_resource("audit", {"events": True}, principal=cli_principal())["items"]

        self.assertLess(len(event["changed"]), 12)
        self.assertEqual(event["more"], 12 - len(event["changed"]))
        listed = self.client.get(event["url"])
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.context["event"].operation_id, OPERATION)

    def test_the_count_of_the_rest_opens_every_item_of_its_operation_a_page_at_a_time(self):
        _items(120)
        html = self.client.get(reverse("core:audit_list")).content.decode()
        (head,) = select(html, "data-folds")
        *_shown, rest = [element for element in within(html, "data-folded", head.attrs["id"]) if element.tag == "a"]

        listed: set[str] = set()
        url = rest.attrs["href"]
        # Each page links the next, until the last; bounded, so a page that links itself cannot loop.
        for _page in range(10):
            html = self.client.get(url).content.decode()
            listed |= set(re.findall(r"\brecord-\d+\b", html))
            following = [link.attrs["href"] for link in select(html, "data-hit") if link.words.startswith("Next")]
            if not following:
                break
            url = f"{reverse('core:audit_list')}{following[0]}"

        self.assertEqual(listed, {f"record-{index}" for index in range(120)})
