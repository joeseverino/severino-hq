"""An event HQ is told of makes the readings its provider declares due, once
per delivery, and makes nothing due that no declaration names."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ReadRequest
from hq.domains.control_plane.observations.portainer import ENVIRONMENT_KIND
from hq.domains.control_plane.tests import bridge_client

# As the Worker forwards a GitHub delivery: the X-GitHub-Event header, and the
# X-GitHub-Delivery GUID GitHub's docs show.
PUSH = {
    "provider": "github_app",
    "event": "push",
    "subject": "example/app",
    "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
    "received_at": "2026-01-15T12:00:00Z",
}

# As severino-hq-events-docker.service tells of one Docker event: the
# container's name, and Docker's own id, time and action as the delivery.
DIED = {
    "provider": "host",
    "event": "docker.container.die",
    "subject": "example-app",
    "delivery_id": "ede54ee1afda366ab42f824e8a5ffd195155d853ceaec74a927f249ea270c743-1461943101381709551-die",
    "received_at": "2016-04-29T15:18:21.381709Z",
}


def worker(*events: dict) -> dict:
    return {"source": "worker", "events": list(events)}


def asked() -> list[str]:
    return sorted(ReadRequest.objects.values_list("kind", flat=True))


class ApplyEventsTests(TestCase):
    def apply(self, batch: dict) -> dict:
        from .. import cadence
        from ..inbound import apply_events

        with mock.patch.object(cadence, "_ring", return_value=True), self.captureOnCommitCallbacks(execute=True):
            return apply_events(batch)

    def test_a_declared_event_is_stored_once_and_makes_its_reading_due(self):
        from hq.domains.control_plane.models import InboundEvent

        answer = self.apply(worker(PUSH))

        event = InboundEvent.objects.get()
        self.assertEqual((event.provider, event.event, event.subject), ("github_app", "push", "example/app"))
        self.assertEqual((event.source, event.outcome), ("worker", "applied"))
        self.assertEqual(asked(), ["github.repository"])
        self.assertEqual(answer["applied"], 1)

    def test_an_action_of_a_declared_event_is_that_event(self):
        self.apply(worker({**PUSH, "event": "pull_request.opened"}))

        self.assertEqual(asked(), ["github.repository"])

    def test_a_container_change_makes_the_container_readings_due(self):
        from hq.domains.control_plane.models import InboundEvent

        for action in ("die", "start", "health_status: unhealthy", "oom", "destroy"):
            event = {**DIED, "event": f"docker.container.{action}", "delivery_id": action}
            self.apply({"source": "host", "events": [event]})

        self.assertEqual(asked(), [ENVIRONMENT_KIND])
        self.assertEqual(set(InboundEvent.objects.values_list("outcome", flat=True)), {"applied"})

    def test_a_host_event_outside_the_declaration_makes_nothing_due(self):
        from hq.domains.control_plane.models import InboundEvent

        for event in ("docker.image.pull", "docker", "container.die", "unit.started.again"):
            self.apply({"source": "host", "events": [{**DIED, "event": event, "delivery_id": event}]})

        self.assertEqual(set(InboundEvent.objects.values_list("outcome", flat=True)), {"ignored"})
        self.assertEqual(asked(), [])

    def test_a_replayed_delivery_asks_nothing(self):
        from hq.domains.control_plane.models import InboundEvent

        self.apply(worker(PUSH))
        ReadRequest.objects.all().delete()

        answer = self.apply(worker(PUSH, PUSH))

        self.assertEqual(InboundEvent.objects.count(), 1)
        self.assertEqual(asked(), [])
        self.assertEqual(answer["repeated"], 2)

    def test_an_event_no_declaration_names_is_ignored(self):
        from hq.domains.control_plane.models import InboundEvent

        self.apply(worker({**PUSH, "event": "star.created"}))

        self.assertEqual(InboundEvent.objects.get().outcome, "ignored")
        self.assertEqual(asked(), [])

    def test_a_batch_off_the_contract_is_refused_whole(self):
        from hq.domains.control_plane.models import InboundEvent

        for batch in (
            worker(PUSH, {**PUSH, "provider": "example.elsewhere", "delivery_id": "other"}),
            worker(PUSH, {**PUSH, "delivery_id": "other", "body": {"commits": []}}),
            {"source": "browser", "events": [PUSH]},
        ):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                self.apply(batch)
        self.assertFalse(InboundEvent.objects.exists())
        self.assertEqual(asked(), [])

    def test_a_batch_with_no_events_says_its_source_is_listening(self):
        from hq.domains.control_plane.models import InboundSource

        from ..inbound_events import channel_standing

        self.assertIsNone(channel_standing(InboundSource.WORKER).seen_at)

        self.apply(worker())

        self.assertIsNotNone(channel_standing(InboundSource.WORKER).seen_at)
        self.assertIsNone(channel_standing(InboundSource.HOST).seen_at)

    def test_a_reconnect_asks_every_reading_the_worker_carries_once_a_minute(self):
        from hq.platform.core.models import UpstreamReading

        reconnected = {**worker(), "reconnected": True}

        answer = self.apply(reconnected)

        self.assertEqual(asked(), ["github.repository"])
        self.assertEqual(answer["asked"], ["github.repository"])
        ReadRequest.objects.all().delete()

        self.apply(reconnected)
        self.apply({**worker(), "reconnected": False})

        self.assertEqual(asked(), [])

        UpstreamReading.objects.update(observed_at=timezone.now() - timedelta(minutes=2))

        self.apply(reconnected)

        self.assertEqual(asked(), ["github.repository"])

    def test_events_past_the_routine_window_are_deleted(self):
        from hq.domains.control_plane.models import InboundEvent

        from ..inbound import prune_events

        self.apply(worker(PUSH, {**PUSH, "delivery_id": "older"}))
        InboundEvent.objects.update(received_at=timezone.now())
        InboundEvent.objects.filter(delivery_id="older").update(received_at=timezone.now() - timedelta(days=400))

        prune_events()

        self.assertEqual(list(InboundEvent.objects.values_list("delivery_id", flat=True)), [PUSH["delivery_id"]])


class BridgeEventsTests(TestCase):
    def test_a_batch_posted_to_the_bridge_makes_its_reading_due(self):
        from hq.domains.control_plane.models import InboundEvent

        with self.captureOnCommitCallbacks(execute=True):
            bridge_client.call("events", worker(PUSH))

        self.assertEqual(InboundEvent.objects.get().delivery_id, PUSH["delivery_id"])
        self.assertEqual(asked(), ["github.repository"])
