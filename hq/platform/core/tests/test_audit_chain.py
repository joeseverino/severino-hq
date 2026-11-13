"""The audit log reads as a chain: every line opens somewhere, a fold opens
only onto rows that do, the order within a minute shows, Who names the
decider, and one record's whole story is one address."""

from datetime import timedelta
from unittest import mock
from uuid import uuid7

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.history import Moment
from hq.platform.application.tests.markup import Element, all_elements, hrefs, select, text
from hq.platform.core import append_only
from hq.platform.core.audit import audited_labels, operation_context
from hq.platform.core.models import AuditLog

# How many records one controller inventory pass changed, the shape that drew
# a fold of rows linking nowhere.
INVENTORY = 90
# The id of the list on an event's page that a fold's count opens.
OPERATION_LIST = "rest-of-operation"


def _inventory(count: int, *, operation: str = "", at=None) -> str:
    """One controller inventory pass that changed ``count`` records and a
    reading, written as the bridge writes it: under an id no row of it names,
    so no row stands for the pass itself."""

    operation = operation or str(uuid7())
    at = at or timezone.now() - timedelta(minutes=5)
    label = audited_labels()[ManagedResource]
    metadata = {"operation": "bridge.inventory", "interface": "controller", "actor": "controller"}
    AuditLog.objects.bulk_create(
        [
            AuditLog(
                action=AuditLog.Action.OBSERVED,
                object_type="Reading",
                object_id="example.rewrite",
                object_repr="example.rewrite (17 records)",
                message="1 record new or changed",
                operation_id=operation,
                metadata=metadata,
                created_at=at,
            ),
            *(
                AuditLog(
                    action=AuditLog.Action.UPDATED,
                    object_type=label,
                    object_id=str(uuid7()),
                    object_repr=f"example-record-{index}",
                    operation_id=operation,
                    metadata={**metadata, "subject": f"example-record-{index}"},
                    created_at=at + timedelta(milliseconds=index + 1),
                )
                for index in range(count)
            ),
        ]
    )
    return operation


def _moments(now) -> list[Moment]:
    """A deploy that links out, one that names no page, a run of container
    starts on one machine (which share its page), and a run of deploys that
    share none."""

    machine = "/infrastructure/machines/example-host/"
    return [
        Moment(now - timedelta(hours=1), "Deploy", "Deployed abc1234 to production", url="https://example.com/d/1"),
        Moment(now - timedelta(hours=2), "Deploy", "Deployed abc1235 to staging"),
        *(
            Moment(
                now - timedelta(hours=3, seconds=index),
                "Container",
                f"example-{index} started on example-host",
                url=f"{machine}#container-example-{index}",
                actor="Docker",
                like=("container", "started on example-host"),
            )
            for index in range(8)
        ),
        *(
            Moment(
                now - timedelta(hours=4, seconds=index),
                "Deploy",
                f"Deployed abc12{index} to production",
                url=f"https://example.com/d/run-{index}",
                actor="GitHub",
                like=("deploy", "of example/repo to production"),
            )
            for index in range(7)
        ),
    ]


def _rows(html: str) -> list[tuple[Element, list[Element], list[str]]]:
    """Each row of the log's table: the cells it draws beyond the select
    column, and every link in it."""

    found = all_elements(html)
    rows = [
        element
        for element in found
        if element.tag == "tr" and not element.beneath and any(a.tag == "tbody" for a in element.ancestors)
    ]
    return [
        (
            row,
            [
                cell
                for cell in found
                if cell.tag == "td"
                and cell.ancestors
                and cell.ancestors[-1] is row
                and "select-column" not in cell.classes
            ],
            [
                element.attrs.get("href", "")
                for element in found
                if element.tag == "a" and any(ancestor is row for ancestor in element.ancestors)
            ],
        )
        for row in rows
    ]


class _Signed(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14))
        with append_only.lifted():
            AuditLog.objects.all().delete()


class EveryLineOpensSomewhereTests(_Signed):
    def test_every_kind_of_line_links_or_declares_it_has_nowhere_to_go(self):
        now = timezone.now()
        _inventory(INVENTORY)
        with operation_context(interface="cli", actor="op", operation="example.one"):
            ManagedResource.objects.create(key="example-lone", kind="example.record", spec={})

        with mock.patch("hq.platform.application.history.external", return_value=_moments(now)):
            page = self.client.get(reverse("core:audit_list"))
        html = page.content.decode()
        lines = page.context["object_list"]
        rows = _rows(html)

        self.assertEqual(len(lines), len(rows))
        kinds = set()
        for line, (_row, cells, links) in zip(lines, rows, strict=True):
            kind = "rest" if line.rest else "burst" if line.burst else "moment" if line.moment else "event"
            kinds.add(kind)
            if line.url:
                self.assertIn(line.url, links, f"a {kind} line opens where it says")
            else:
                # Only what another source dated can have nowhere in HQ to go.
                self.assertIn(kind, {"moment", "burst"}, f"a {kind} line with nowhere to go")
            if kind == "rest":
                # The count of the rest is one cell across the row, never a run of empty ones.
                self.assertEqual(len(cells), 1)
        self.assertEqual(kinds, {"event", "burst", "moment", "rest"})

    def test_a_fold_opens_only_onto_rows_that_link(self):
        now = timezone.now()
        _inventory(INVENTORY)

        with mock.patch("hq.platform.application.history.external", return_value=_moments(now)):
            html = self.client.get(reverse("core:audit_list")).content.decode()

        rows = _rows(html)
        heads = [row.attrs["id"] for row, _cells, _links in rows if "data-folds" in row.attrs]
        self.assertTrue(heads)
        for head in heads:
            folded = [links for row, _cells, links in rows if row.attrs.get("data-folded") == head]
            self.assertTrue(folded)
            for links in folded:
                self.assertTrue(links, f"a row folded under {head} links somewhere")

    def test_a_pass_no_row_stands_for_opens_its_newest_row_and_its_count_opens_the_list_of_all(self):
        _inventory(INVENTORY)
        newest = AuditLog.objects.order_by("-created_at", "-pk").first()
        page = reverse("core:audit_detail", args=[newest.pk])

        html = self.client.get(reverse("core:audit_list")).content.decode()

        (head_links,) = [links for row, _cells, links in _rows(html) if "data-folds" in row.attrs]
        self.assertIn(page, head_links)
        self.assertIn(f"{page}#{OPERATION_LIST}", hrefs(html))
        # That list is on the page it opens.
        listed = self.client.get(page).content.decode()
        self.assertEqual(len(select(listed, "id", OPERATION_LIST)), 1)


class ChainBudgetTests(_Signed):
    def test_a_page_holding_a_90_record_pass_costs_what_one_of_9_does(self):
        def cost():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(reverse("core:audit_list"))
            return len(queries)

        operation = _inventory(9)
        cost()
        small = cost()
        _inventory(INVENTORY - 9, operation=operation, at=timezone.now() - timedelta(minutes=4))
        large = cost()

        self.assertEqual(large, small)


class TimeAndWhoTests(_Signed):
    def test_events_within_one_minute_say_their_seconds(self):
        minute = timezone.now().replace(second=5, microsecond=0) - timedelta(hours=1)
        AuditLog.objects.bulk_create(
            AuditLog(
                action=AuditLog.Action.CREATED,
                object_type="example.record",
                object_repr=f"example-{second}",
                created_at=minute + timedelta(seconds=second),
            )
            for second in (0, 30)
        )

        page = self.client.get(reverse("core:audit_list"))
        times = [element.words for element in all_elements(page.content.decode()) if element.tag == "time"]

        self.assertEqual(len(set(times)), len(times))

    def test_who_names_the_decider_beside_the_actor(self):
        AuditLog.objects.create(
            action=AuditLog.Action.CREATED,
            object_type="example.record",
            object_repr="example-approved",
            metadata={"actor": "example-agent", "approved_by": "owner"},
        )

        shown = text(self.client.get(reverse("core:audit_list")))

        self.assertIn("example-agent · approved by owner", shown)

    def test_a_row_its_actor_approved_names_them_once(self):
        AuditLog.objects.create(
            action=AuditLog.Action.CREATED,
            object_type="example.record",
            object_repr="example-self",
            metadata={"actor": "example-agent", "approved_by": "example-agent"},
        )

        self.assertNotIn("approved by", text(self.client.get(reverse("core:audit_list"))))


class RecordStoryTests(_Signed):
    def test_about_a_record_shows_its_rows_and_no_other_records(self):
        with operation_context(interface="cli", actor="op", operation="example.one"):
            ManagedResource.objects.create(key="example-mine", kind="example.record", spec={})
            ManagedResource.objects.create(key="example-other", kind="example.record", spec={})

        page = self.client.get(reverse("core:audit_list"), {"about": "example-mine"})
        shown = text(page)

        self.assertIn("example-mine", shown)
        self.assertNotIn("example-other", shown)

    def test_a_records_page_links_to_its_whole_story(self):
        resource = ManagedResource.objects.create(key="example-story", kind="machine", spec={})

        page = self.client.get(reverse("control_plane:detail", args=[resource.key]))

        self.assertIn(f"{reverse('core:audit_list')}?about=example-story", hrefs(page))

    def test_an_events_page_names_what_started_it_and_the_record_it_is_about(self):
        cause = str(uuid7())
        started = AuditLog.objects.create(
            action=AuditLog.Action.CREATED,
            object_type="Infrastructure operation",
            object_id=cause,
            object_repr="example-story: reconcile",
            operation_id=cause,
        )
        event = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED,
            object_type="Managed resource",
            object_repr="example-story",
            operation_id=str(uuid7()),
            metadata={"cause": cause, "subject": "example-story"},
        )

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))
        links = hrefs(page)

        self.assertIn(reverse("core:audit_detail", args=[started.pk]), links)
        self.assertIn(f"{reverse('core:audit_list')}?about=example-story", links)
        self.assertIn("example-story: reconcile", text(page))
        # Said in words, never as the stored ids.
        self.assertEqual(set(page.context["extra"]), set())
