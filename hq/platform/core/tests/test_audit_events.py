"""The audit log reads as events: one line per operation, its items beneath it."""

import re

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.domains.jobs.runner import run
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.domains import domain_attention_items
from hq.platform.application.read_models import recent_activity
from hq.platform.application.security import Capability, Principal, cli_principal
from hq.platform.application.tests.markup import select, within
from hq.platform.application.tests.writers import keyed
from hq.platform.core import append_only
from hq.platform.core.audit import audit_operation, record_event
from hq.platform.core.models import AuditLog

# Written out, so the module imports at a ref without the names.
SCHEDULER = "timer"


def _changes_two(progress) -> dict:
    for name in ("example-a", "example-b"):
        record_event(action=AuditLog.Action.UPDATED, type_label="example.record", message=f"Changed {name}")
    return {}


class AuditEventPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14))
        with append_only.lifted():
            AuditLog.objects.all().delete()

    def page(self, **params):
        return self.client.get(reverse("core:audit_list"), params)

    def test_a_run_is_one_line_with_what_it_changed_beneath_it(self):
        job = run("example.sweep", "Sweep the examples", _changes_two, actor=SCHEDULER)

        response = self.page()
        (event,) = [line for line in response.context["events"] if not line.under]
        self.assertEqual(event.title, "Sweep the examples")
        self.assertEqual((event.actor, event.action), (SCHEDULER, "done"))
        self.assertEqual(len(event.items), 2)
        # The items are rows of the same table, folded under the event's own row.
        html = response.content.decode()
        (head,) = select(html, "data-folds")
        self.assertEqual(len(select(html, "data-folded", head.attrs["id"])), 2)
        # An operation is named, never shown as its id.
        self.assertNotIn(str(job.pk), html)

    def test_a_scheduled_run_that_changed_nothing_waits_behind_its_toggle(self):
        run("example.check", "Check the examples", lambda progress: {"seen": 3}, actor=SCHEDULER)

        def titles(**params):
            return [line.title for line in self.page(**params).context["events"]]

        self.assertNotIn("Check the examples", titles())
        self.assertIn("Check the examples", titles(routine=1))

    def test_rows_of_one_operation_are_one_event_on_one_page(self):
        principal = cli_principal()
        for index in range(60):
            with audit_operation(operation="example.edit", principal=principal):
                record_event(action=AuditLog.Action.UPDATED, type_label="example.record", message=f"one {index}")
                record_event(action=AuditLog.Action.UPDATED, type_label="example.record", message=f"two {index}")

        first, second = self.page().context, self.page(page=2).context
        heads = [[line for line in context["events"] if not line.under] for context in (first, second)]

        # Fifty events a page, each with both its rows, and none on two pages.
        self.assertEqual([len(lines) for lines in heads], [50, 10])
        self.assertTrue(all(len(line.events) == 2 for lines in heads for line in lines))
        operations = [line.events[0].operation_id for lines in heads for line in lines]
        self.assertEqual(len(set(operations)), 60)
        self.assertTrue(first["page_obj"].has_next())
        self.assertFalse(second["page_obj"].has_next())

    def test_a_page_counts_nothing(self):
        for index in range(3):
            with audit_operation(operation="example.edit", principal=cli_principal()):
                record_event(action=AuditLog.Action.UPDATED, type_label="example.record", message=f"one {index}")

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.page().status_code, 200)

        reads = [query["sql"] for query in queries.captured_queries if '"core_auditlog"' in query["sql"]]
        # An event counts what it changed, within its operation; nothing counts the log.
        self.assertFalse([sql for sql in reads if re.search(r"COUNT\([^)]*\)(?!\s*OVER)", sql, re.IGNORECASE)])

    def test_a_row_of_no_operation_is_an_event_of_its_own(self):
        for index in range(3):
            AuditLog.objects.create(
                action=AuditLog.Action.UPDATED, object_type="example.record", object_repr=f"r{index}"
            )

        self.assertEqual([len(line.events) for line in self.page().context["events"]], [1, 1, 1])


class EventPageTests(TestCase):
    """An event's page states each fact once, under a label that names one thing."""

    def setUp(self):
        person = get_user_model().objects.create_superuser("owner", "owner@example.test", "x" * 14)
        self.client.force_login(person)
        self.job = run(
            "example.import",
            "Import the examples",
            lambda progress: {"created": 3, "seen": 5, "source": "example.csv"},
            actor="owner",
            requested_by=person,
        )
        self.row = AuditLog.objects.get(object_type="Background job", object_id=str(self.job.pk))

    def facts(self, row=None) -> list[tuple[str, str]]:
        """Each label on the event's page and what it says, in order."""

        html = self.client.get(reverse("core:audit_detail", args=[(row or self.row).pk])).content.decode()
        found = []
        for region in ("Event", "Recorded with it"):
            elements = [
                element for element in within(html, "aria-label", region) if element.tag in {"dt", "dd", "th", "td"}
            ]
            found += [(dt.words, dd.words) for dt, dd in zip(elements[::2], elements[1::2], strict=True)]
        return found

    def labels(self) -> list[str]:
        return [label for label, _value in self.facts()]

    def test_no_label_is_given_twice(self):
        labels = self.labels()

        self.assertTrue(labels)
        self.assertEqual(sorted(labels), sorted(set(labels)))

    def test_what_a_row_or_facet_says_is_not_recorded_again_beside_it(self):
        labels = self.labels()

        # Who acted is the Who row; the counts are the Counts facet's.
        self.assertIn("Who", labels)
        self.assertNotIn("Actor", labels)
        self.assertIn("Counts", labels)
        self.assertFalse({"Created", "Seen", "Job", "Kind"} & set(labels))

    def test_who_acted_is_said_with_the_door_they_came_through(self):
        facts = dict(self.facts())

        # Written out, so the module imports at a ref without the words.
        self.assertEqual(facts["Who"], "owner, in a background job")
        self.assertNotIn("Interface", facts)

    def test_an_event_with_nothing_else_recorded_has_no_card_for_it(self):
        with audit_operation(operation="example.edit", principal=cli_principal()):
            row = record_event(action=AuditLog.Action.UPDATED, type_label="example.record", required=True)

        html = self.client.get(reverse("core:audit_detail", args=[row.pk])).content.decode()

        self.assertEqual(dict(self.facts(row))["Who"], "local-operator, at the shell")
        self.assertFalse(within(html, "aria-label", "Recorded with it"))


class RecentActivityTests(TestCase):
    def setUp(self):
        with append_only.lifted():
            AuditLog.objects.all().delete()

    def test_a_model_reads_the_events_a_person_does(self):
        run("example.sweep", "Sweep the examples", _changes_two, actor=SCHEDULER)
        run("example.check", "Check the examples", lambda progress: {}, actor=SCHEDULER)

        (event,) = recent_activity(principal=cli_principal())["items"]

        self.assertEqual((event["title"], event["actor"], event["action"]), ("Sweep the examples", SCHEDULER, "done"))
        self.assertEqual(sorted(item["message"] for item in event["items"]), ["Changed example-a", "Changed example-b"])


@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True)
class HeldChangeGroupTests(TestCase):
    def test_a_held_change_is_grouped_as_waiting_for_approval(self):
        CapabilityRule.objects.create(scope="surface", subject="mcp", capability="documentation.sync", rule="approve")
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.SYNC_DOCUMENTATION}))
        held = execute_capability(
            "documentation.sync", keyed({"manifest": [{"doc_id": "rb-example", "title": "Example"}]}), principal=agent
        )

        (item,) = [
            item for item in domain_attention_items() if item["item"].key == f"approval:{held['approval']['id']}"
        ]

        self.assertIn("approval", item["source"].lower())
        self.assertNotEqual(item["source"], "Audit")
