"""Each bridge socket serves only its own lane: the automation worker's
leases and ends the automation kinds' work, through its claim, renewal and
report alone, the controller's every other kind's, and the event readers'
takes events alone. All run as one account and name one host, so the socket
is all that tells them apart."""

import json
from datetime import timedelta
from typing import Any

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import operations
from hq.domains.control_plane.models import OperationRequest
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.testing import declare

from . import bridge_client

# The one name the controller and the worker both claim under: the host's.
HOST = "example-host"
QUEUE = {"claim", "renew", "report"}
AUTOMATION = ("automation.operation", "automation.source")


def claimed(kind: str, action: str = OperationRequest.Action.RUN) -> OperationRequest:
    """Work on a record of ``kind`` that ``HOST`` holds the lease of."""

    now = timezone.now()
    return OperationRequest.objects.create(
        resource=declare(f"example-{kind.replace('.', '-')}", kind),
        action=action,
        idempotency_key=f"example-{kind}-{action}",
        input={"generation": 1},
        state=OperationRequest.State.CLAIMED,
        claimed_by=HOST,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )


def ask(app: Any, action: str, operation: OperationRequest | None = None, **body: Any) -> bridge_client.Answer:
    query: dict[str, Any] = {"controller-id": HOST}
    if operation is not None:
        query["operation"] = str(operation.pk)
    return bridge_client.request(f"/{action}", query=query, body=json.dumps(body).encode() if body else b"", app=app)


def claim(app: Any, kind: str, action: str) -> bridge_client.Answer:
    return bridge_client.request("/claim", query={"controller-id": HOST, "capability": f"{kind}:{action}"}, app=app)


class LaneTestCase(TestCase):
    def refused(self, answer: bridge_client.Answer, operation: OperationRequest | None = None) -> None:
        self.assertEqual(answer.status, 400, answer.body)
        self.assertEqual(answer.media_type, "application/problem+json")
        if operation is not None:
            held = OperationRequest.objects.get(pk=operation.pk)
            self.assertEqual(held.state, OperationRequest.State.CLAIMED)
            self.assertEqual(held.lease_expires_at, operation.lease_expires_at)


class WorkerSocketTests(LaneTestCase):
    def test_it_serves_no_action_but_the_claim_renewal_and_report(self):
        for action in sorted(set(operations()) - QUEUE):
            with self.subTest(action=action):
                answer = bridge_client.request(f"/{action}", query={"controller-id": HOST}, app=bridge_client.worker())
                self.assertEqual(answer.status, 404, answer.body)

    def test_it_claims_no_work_of_a_kind_outside_the_automation_kinds(self):
        for kind in sorted(set(PROVIDERS) - set(AUTOMATION)):
            with self.subTest(kind=kind):
                self.refused(claim(bridge_client.worker(), kind, "reconcile"))
        self.assertEqual(claim(bridge_client.worker(), "automation.operation", "run").status, 200)

    def test_it_renews_and_reports_no_work_of_another_kind(self):
        for kind, action in (("tls.certificate", "reconcile"), ("github.pull_request", "merge")):
            operation = claimed(kind, action)
            with self.subTest(kind=kind):
                self.refused(ask(bridge_client.worker(), "renew", operation), operation)
                self.refused(
                    ask(bridge_client.worker(), "report", operation, success=True, observed_generation=1), operation
                )


class EventsSocketTests(LaneTestCase):
    def test_it_serves_no_action_but_the_events(self):
        for action in sorted(set(operations()) - {"events"}):
            with self.subTest(action=action):
                answer = bridge_client.request(f"/{action}", query={"controller-id": HOST}, app=bridge_client.events())
                self.assertEqual(answer.status, 404, answer.body)

    def test_the_controllers_socket_takes_no_events(self):
        answer = bridge_client.request("/events", body=b'{"source":"host","events":[]}', app=bridge_client.application)
        self.assertEqual(answer.status, 404, answer.body)


class ControllerSocketTests(LaneTestCase):
    def test_it_claims_renews_and_reports_none_of_the_automation_kinds(self):
        for kind, action in (("automation.operation", "run"), ("automation.source", "scan")):
            operation = claimed(kind, action)
            with self.subTest(kind=kind):
                self.refused(claim(bridge_client.application, kind, action))
                self.refused(ask(bridge_client.application, "renew", operation), operation)
                self.refused(
                    ask(bridge_client.application, "report", operation, success=False, observed_generation=1),
                    operation,
                )

    def test_a_merge_reported_done_is_taken_from_the_controllers_socket_alone(self):
        merge = claimed("github.pull_request", "merge")
        done = {"success": True, "observed_generation": 1, "status": {"merge_sha": "a" * 40}}

        self.refused(ask(bridge_client.worker(), "report", merge, **done), merge)
        merge.resource.refresh_from_db()
        self.assertEqual(merge.resource.status, {})

        answer = ask(bridge_client.application, "report", merge, **done)

        self.assertEqual(answer.status, 200, answer.body)
        merge.refresh_from_db()
        self.assertEqual(merge.state, OperationRequest.State.SUCCEEDED)
