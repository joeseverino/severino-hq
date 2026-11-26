"""What HQ was told happened outside it, on its page, and the listener's
standing beside the controller's. Its list read is held with every other
by test_list_paging."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource
from hq.platform.application import readings
from hq.platform.application.inbound_events import channel
from hq.platform.core.testing import operator

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


def heard(source: InboundSource) -> None:
    readings.record(readings.heartbeat(channel(source)), {})


@SOURCE
class EventsPageTests(TestCase):
    def setUp(self):
        operator(self.client)

    def test_events_are_newest_first_under_the_channel_each_came_by(self):
        told(InboundSource.WORKER, "example.old", hours=5)
        told(InboundSource.HOST, "example.host", hours=1)
        told(InboundSource.WORKER, "example.new", hours=2, outcome=InboundOutcome.IGNORED)

        table = self.client.get(reverse("control_plane:events")).context["table"]["drawn"]

        self.assertEqual(
            [(group.label, [row.cells[0].text for row in group.rows]) for group in table.groups],
            [("Worker", ["example.new", "example.old"]), ("Host", ["example.host"])],
        )

    def test_a_listener_never_heard_points_to_its_setup_and_the_host_is_not_called_unset(self):
        empty = self.client.get(reverse("control_plane:events"))
        heard(InboundSource.WORKER)
        listening = self.client.get(reverse("control_plane:events"))

        self.assertContains(empty, "No events yet.")
        self.assertIn(GUIDE, empty.context["page_facts"])
        self.assertIn("Host: Nothing yet", strip_tags(empty.context["page_facts"]))
        self.assertNotIn(GUIDE, listening.context["page_facts"])
        self.assertIn("heard just now", strip_tags(listening.context["page_facts"]))

    def test_the_controller_page_shows_the_listener_beside_the_controller(self):
        never = self.client.get(reverse("control_plane:controller"))
        heard(InboundSource.WORKER)
        arrived = self.client.get(reverse("control_plane:controller"))

        self.assertContains(never, GUIDE)
        self.assertNotContains(arrived, GUIDE)
        self.assertIn("Last heard just now.", strip_tags(arrived.content.decode()))
        self.assertContains(arrived, reverse("control_plane:events"))
