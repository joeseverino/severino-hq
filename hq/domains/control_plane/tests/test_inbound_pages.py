"""What HQ was told happened outside it, on its page, and the listener's
standing beside the controller's. Its list read is held with every other
by test_list_paging."""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource
from hq.domains.control_plane.provider_adapters import INBOUND
from hq.domains.control_plane.provider_spec import InboundSpec
from hq.domains.control_plane.providers import registry_label
from hq.platform.application.entity_links import kind_label
from hq.platform.core.testing import channel_heard, operator

SOURCE = override_settings(SEVERINO_HQ_SOURCE="https://github.com/example/hq")
GUIDE = "docs/Install.md#12-hear-providers-events"


def told(source: InboundSource, event: str, *, hours: int, outcome: InboundOutcome = InboundOutcome.APPLIED) -> None:
    InboundEvent.objects.create(
        provider="example",
        event=event,
        subject="example/site",
        delivery_id=f"{source}-{event}",
        source=source,
        received_at=timezone.now() - timedelta(hours=hours),
        applied_at=timezone.now() - timedelta(hours=hours),
        outcome=outcome,
    )


@SOURCE
class EventsPageTests(TestCase):
    def setUp(self):
        operator(self.client)

    def test_events_are_newest_first_under_the_channel_each_came_by_in_that_order(self):
        told(InboundSource.WORKER, "example.old", hours=5)
        told(InboundSource.HOST, "example.host", hours=1)
        told(InboundSource.WORKER, "example.new", hours=2, outcome=InboundOutcome.IGNORED)

        table = self.client.get(reverse("control_plane:events")).context["table"]["drawn"]

        self.assertEqual(
            [(group.label, [row.cells[0].text for row in group.rows]) for group in table.groups],
            [
                (InboundSource.HOST.label, ["example.host"]),
                (InboundSource.WORKER.label, ["example.new", "example.old"]),
            ],
        )

    def test_what_hq_takes_is_each_declared_event_under_its_provider_by_what_it_makes_due(self):
        declared = InboundSpec(
            {
                "example.started": ("example.reading",),
                "example.stopped": ("example.reading",),
                "example.moved": ("example.place",),
            },
            complete=("example.place",),
        )
        with mock.patch.dict(INBOUND, {"example": declared}):
            taken = self.client.get(reverse("control_plane:events")).context["taken"]

        group = next(group for group in taken.groups if group.label == "Example")
        self.assertEqual(group.count, 3)
        self.assertEqual(
            [tuple(cell.text for cell in row.cells[:3]) for row in group.rows],
            [
                ("example.started, example.stopped", kind_label("example.reading"), ""),
                ("example.moved", kind_label("example.place"), kind_label("example.place")),
            ],
        )
        # A source that reports over the bridge alone arrives by HQ's host jobs.
        self.assertEqual({row.cells[3].text for row in group.rows}, {InboundSource.HOST.label})

    def test_a_listener_never_heard_points_to_its_setup_and_the_host_is_not_called_unset(self):
        empty = self.client.get(reverse("control_plane:events"))
        channel_heard(InboundSource.WORKER)
        listening = self.client.get(reverse("control_plane:events"))

        self.assertContains(empty, "No events yet.")
        self.assertIn(GUIDE, empty.context["page_facts"])
        self.assertIn("Host: Nothing yet", strip_tags(empty.context["page_facts"]))
        self.assertNotIn(GUIDE, listening.context["page_facts"])
        self.assertIn("seen just now", strip_tags(listening.context["page_facts"]))

    def test_the_controller_page_shows_the_listener_beside_the_controller(self):
        never = self.client.get(reverse("control_plane:controller"))
        channel_heard(InboundSource.WORKER)
        arrived = self.client.get(reverse("control_plane:controller"))

        self.assertContains(never, GUIDE)
        self.assertNotContains(arrived, GUIDE)
        self.assertIn("Last seen just now.", strip_tags(arrived.content.decode()))
        self.assertContains(arrived, reverse("control_plane:events"))

    def test_the_listener_gone_silent_stands_on_events_and_the_controller(self):
        channel_heard(InboundSource.WORKER, silent=True)

        for page in ("control_plane:events", "control_plane:controller"):
            with self.subTest(page):
                problems = self.client.get(reverse(page)).context["problems"]
                self.assertIn("The event listener stopped reporting", [item["label"] for item in problems])


class MadeDueTests(SimpleTestCase):
    def test_every_reading_a_declared_event_makes_due_is_named(self):
        unnamed = registry_label("example.undeclared")
        for provider, spec in INBOUND.items():
            for kind in {kind for kinds in spec.events.values() for kind in kinds}:
                with self.subTest(provider=provider, kind=kind):
                    self.assertNotEqual(kind_label(kind), unnamed)
