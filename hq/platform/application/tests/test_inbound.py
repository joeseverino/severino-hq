"""An event HQ is told of makes the readings its provider declares due, once
per delivery, and makes nothing due that no declaration names."""

import json
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
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

# As severino-hq-events.service tells of one Docker event: the
# container's name, and Docker's own id, time and action as the delivery.
DIED = {
    "provider": "host",
    "event": "docker.container.die",
    "subject": "example-app",
    "delivery_id": "ede54ee1afda366ab42f824e8a5ffd195155d853ceaec74a927f249ea270c743-1461943101381709551-die",
    "received_at": "2016-04-29T15:18:21.381709Z",
}


# What a GitHub delivery makes due: the repositories it is about, and the
# webhook's delivery log, which now holds a delivery that arrived.
PUSHED = ["github.open_pull_request", "github.repository", "github.webhook", "github.workflow_run"]


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
        self.assertEqual(asked(), PUSHED)
        self.assertEqual(answer["applied"], 1)

    def test_an_action_of_a_declared_event_is_that_event(self):
        self.apply(worker({**PUSH, "event": "pull_request.opened"}))

        self.assertEqual(asked(), PUSHED)

    def test_a_container_change_makes_the_container_readings_due(self):
        """Each container reading a change moves, and once the controller
        stores a recreated container running a digest HQ has not read, the
        registry read starts, once."""

        from hq.domains.control_plane.models import InboundEvent

        from ..report_testing import bridged
        from .test_containers import running

        for action in ("die", "start", "health_status: unhealthy", "oom", "destroy"):
            event = {**DIED, "event": f"docker.container.{action}", "delivery_id": action}
            self.apply({"source": "host", "events": [event]})

        self.assertEqual(
            asked(), sorted([ENVIRONMENT_KIND, "portainer.container", "portainer.image", "portainer.runtime"])
        )
        self.assertEqual(set(InboundEvent.objects.values_list("outcome", flat=True)), {"applied"})

        recreated = running("example-app", "ghcr.io/example/app:v2@sha256:" + "c" * 64)
        with (
            mock.patch("hq.platform.application.outbound_work.start_for_host") as start,
            self.captureOnCommitCallbacks(execute=True),
        ):
            bridged("inventory", {"portainer.container": {"ok": True, "records": [recreated]}})
        start.assert_called_once_with("registry.refresh")

    def test_a_job_systemd_ended_failed_makes_the_unit_reading_due(self):
        """For any unit on the host, HQ's or not, as severino-hq-events.service
        tells of systemd's JobRemoved: the unit's name, and the job's id and
        when it was heard as the delivery."""

        self.apply({"source": "host", "events": [{
            "provider": "host", "event": "unit.job.failed", "subject": "example-pull@7ad6909.service",
            "delivery_id": "job-4021-1764158400000000000", "received_at": "2026-11-26T12:00:00Z",
        }]})  # fmt: skip

        self.assertEqual(asked(), ["host.unit"])

    def test_a_cloudflare_alert_makes_its_reading_and_the_alert_history_due(self):
        """As the Worker forwards a Cloudflare notification: its alert_type,
        the account it fired for, and the body's digest as the delivery."""

        self.apply(worker({
            "provider": "cloudflare_api", "event": "tunnel_health_event", "subject": "0123abcd",
            "delivery_id": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "received_at": "2026-11-26T12:00:00Z",
        }))  # fmt: skip

        self.assertEqual(asked(), ["cloudflare.alert", "cloudflare.tunnel"])

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

    def test_a_reconnect_asks_every_reading_the_worker_carries(self):
        from hq.domains.control_plane.provider_adapters import INBOUND

        routed = sorted(
            {kind for spec in INBOUND.values() if spec.path for kinds in spec.events.values() for kind in kinds}
        )

        answer = self.apply({**worker(), "reconnected": True})

        self.assertEqual(asked(), routed)
        self.assertEqual(sorted(answer["asked"]), routed)
        ReadRequest.objects.all().delete()

        self.apply({**worker(), "reconnected": False})

        self.assertEqual(asked(), [])

    def test_two_events_for_one_repository_make_one_read_of_it(self):
        from ..read_requests import forced_reads

        self.apply(worker(PUSH, {**PUSH, "event": "pull_request.opened", "delivery_id": "second"}))

        reads = forced_reads()
        self.assertEqual(sorted(read.kind for read in reads), PUSHED)
        self.assertEqual({read.subject for read in reads}, {"example/app"})

    def test_an_ask_made_after_a_pass_took_its_verdict_is_read_by_the_next(self):
        from hq.domains.control_plane.models import ProviderInventory

        from ..cadence import sweep_due
        from ..derivation_testing import frozen
        from ..read_requests import forced_reads, take

        start = timezone.now()

        @contextmanager
        def at(seconds):
            moment = start + timedelta(seconds=seconds)
            with frozen(moment), mock.patch("django.utils.timezone.now", return_value=moment):
                yield

        with at(-3600):
            for kind in PUSHED:
                ProviderInventory.objects.create(kind=kind, records=[], reachable=True)
        with at(0):
            self.apply(worker(PUSH))
        with at(1):
            sweep_due("example-controller", take(forced_reads()))
        with at(2):
            self.apply(worker({**PUSH, "event": "pull_request.opened", "delivery_id": "after"}))
        # The pass that read before the second ask stores after it.
        with at(3):
            ProviderInventory.objects.update(observed_at=timezone.now(), updated_at=timezone.now())
            self.assertEqual(sorted(read.kind for read in forced_reads()), PUSHED)
            self.assertTrue(sweep_due()["due"])

    def test_each_declared_fact_is_told_from_its_example_until_its_subject_is_read(self):
        """Walks every fact a declaration gives an event, from its example
        body as the Worker forwards it; a replay of an older body stands
        where it was said, and the subject's next read ends what was told."""

        from hq.domains.control_plane.provider_adapters import INBOUND
        from hq.domains.control_plane.provider_spec import pointed

        from ..inbound_events import told
        from ..moments import moment

        walked = 0
        for provider, spec in INBOUND.items():
            for event, fact in spec.facts.items():
                with self.subTest(provider=provider, event=event):
                    said = fact.read(fact.example)
                    action = pointed(fact.example, spec.action_pointer) if spec.action_pointer else ""
                    subject = pointed(fact.example, spec.subject_pointer)
                    arrived = {
                        **PUSH,
                        "provider": provider,
                        "event": f"{event}.{action}" if action else event,
                        "subject": subject,
                        "delivery_id": f"fact-{walked}",
                        **said,
                    }
                    self.assertEqual(self.apply(worker(arrived))["applied"], 1)
                    self.assertFalse(ReadRequest.objects.exists(), "a fact that draws asks nothing")
                    replayed = {**arrived, "delivery_id": f"replayed-{walked}", "said_at": "2000-01-01T00:00:00Z"}
                    self.apply(worker(replayed))

                    *_, newest = told(provider, subject, None)[said["about"]]
                    self.assertEqual(newest.fact, fact.shape.model_validate(said["fact"]))
                    self.assertEqual(told(provider, subject, moment(said["said_at"])), {})
                    walked += 1
        self.assertTrue(walked, "a declaration gives an event a fact")

    def test_events_stored_before_the_routine_window_are_deleted_whatever_their_sender_said(self):
        from hq.domains.control_plane.models import InboundEvent

        from ..inbound import prune_events

        self.apply(worker(PUSH, {**PUSH, "delivery_id": "older"}))
        InboundEvent.objects.update(received_at=timezone.now() - timedelta(days=400))
        InboundEvent.objects.filter(delivery_id="older").update(applied_at=timezone.now() - timedelta(days=400))

        prune_events()

        self.assertEqual(list(InboundEvent.objects.values_list("delivery_id", flat=True)), [PUSH["delivery_id"]])

    def test_a_pruned_event_leaves_no_row_behind(self):
        from hq.domains.control_plane.models import InboundEvent
        from hq.platform.core.models import AuditLog

        from ..inbound import prune_events

        self.apply(worker(PUSH))
        told = str(InboundEvent.objects.get())
        InboundEvent.objects.update(applied_at=timezone.now() - timedelta(days=400))

        prune_events()

        self.assertFalse(InboundEvent.objects.exists())
        self.assertFalse(AuditLog.objects.filter(object_repr=told).exists())


class BridgeEventsTests(TestCase):
    def test_a_batch_posted_to_the_bridge_makes_its_reading_due(self):
        from hq.domains.control_plane.models import InboundEvent

        with self.captureOnCommitCallbacks(execute=True):
            bridge_client.call("events", worker(PUSH), app=bridge_client.events())

        self.assertEqual(InboundEvent.objects.get().delivery_id, PUSH["delivery_id"])
        self.assertEqual(asked(), PUSHED)


# The tag worker:deploy gives the Worker's version, read where it reads it,
# and an older deploy's tag.
TAG = (Path(settings.BASE_DIR) / "worker/src/generated/deploy-tag").read_text().strip()
OLD = "fedcba9876543210fedcba9876543210"


class EventsWorkerTests(TestCase):
    def test_a_worker_serving_a_version_of_other_routes_is_behind_however_the_deployment_serves_hq(self):
        from hq.platform.application.inbound_events import events_worker
        from hq.platform.application.inventory_testing import store
        from hq.platform.core.stylesheets import SHEETS

        with tempfile.TemporaryDirectory() as collected:
            # As the image's collectstatic leaves it: each sheet named by its content.
            hashed = {sheet: sheet.replace(".css", ".0123456789ab.css") for sheet in SHEETS}
            (Path(collected) / "staticfiles.json").write_text(json.dumps({"version": "1.1", "paths": hashed}))
            image = {"BACKEND": "hq.platform.core.static.HashedStaticStorage"}
            for served in (
                {},
                {"STORAGES": {**settings.STORAGES, "staticfiles": image}, "STATIC_ROOT": collected},
                {"SEVERINO_SITE_NAME": "Example HQ"},
            ):
                for tags, behind in (((TAG,), False), ((OLD,), True), ((TAG, OLD), True), ((), False)):
                    with self.subTest(served=sorted(served), tags=tags), override_settings(**served):
                        versions = [
                            {"id": f"example-{tag}", "number": 1, "percentage": 100 / len(tags), "tag": tag}
                            for tag in tags
                        ]
                        store(
                            "cloudflare.worker",
                            {"connection_ref": "example-cloudflare", "name": "hq-events", "versions": versions},
                        )

                        self.assertEqual(events_worker().behind, behind)

    def test_each_route_the_worker_serves_is_counted_by_what_it_answered(self):
        from hq.platform.application.inbound_events import events_worker
        from hq.platform.application.inventory_testing import store

        answers = [
            {"route": "/github", "status": 202, "requests": 40, "last_hour": 5},
            {"route": "/github", "status": 401, "requests": 3, "last_hour": 1},
            {"route": "/github", "status": 413, "requests": 2, "last_hour": 0},
            {"route": "/socket", "status": 101, "requests": 4, "last_hour": 0},
            {"route": "/example-retired", "status": 401, "requests": 9, "last_hour": 9},
        ]
        store("cloudflare.worker", {"connection_ref": "example-cloudflare", "name": "hq-events", "answers": answers})

        routes = {route.path: route for route in events_worker().routes}

        # Every route it serves, counted or not, and none it no longer serves.
        self.assertEqual(set(routes), {"/github", "/tailscale", "/cloudflare", "/socket"})
        github = routes["/github"]
        self.assertEqual((github.accepted, github.refused, github.unsigned), (40, 5, 3))
        self.assertEqual((github.accepted_last_hour, github.unsigned_last_hour), (5, 1))
        self.assertEqual((routes["/socket"].accepted, routes["/tailscale"].refused), (4, 0))
        self.assertEqual(github.secret, "GITHUB_WEBHOOK_SECRET")

    def test_a_worker_whose_answers_were_not_read_counts_no_route(self):
        from hq.platform.application.inbound_events import events_worker
        from hq.platform.application.inventory_testing import store

        store("cloudflare.worker", {"connection_ref": "example-cloudflare", "name": "hq-events"})

        self.assertIsNone(events_worker().routes)
