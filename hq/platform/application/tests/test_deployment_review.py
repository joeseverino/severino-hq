"""Approve and Reject answer a run waiting on HQ's deployment protection rule:
a person's press declares the answer at the commit seen and queues the
controller's reconcile, an agent's waits for a person, and a run that is not
waiting there at that commit is refused before anything is queued."""

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ManagedResource, OperationRequest

from ..capabilities import run_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal
from .test_github_estate import store

APPROVE = "deployment.approve"
REJECT = "deployment.reject"
KIND = "github.deployment_review"
HEAD = "d" * 40
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
RUN = {"id": 41, "name": "Deploy", "environments": ["release"], "sha": HEAD, "created_at": "2030-01-01T00:00:00Z"}
# The environment, as the repository's reading holds it with HQ's App as its rule.
RELEASE = {"name": "release", "hq_rule": True}
ASKED = {"repository": "example/alpha", "run_id": 41, "environment": "release", "head_sha": HEAD}


class ReviewTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        store(waiting=[RUN], environments=[RELEASE])

    def press(self, capability=APPROVE, principal=PERSON, key="example-review-1", **changes):
        return run_capability(capability, {**ASKED, **changes}, principal=principal, idempotency_key=key).answer

    def test_a_persons_answer_is_declared_at_the_commit_and_reconciled_by_the_controller(self):
        for capability, state in ((APPROVE, "approved"), (REJECT, "rejected")):
            with self.subTest(state=state):
                answer = self.press(capability, key=f"example-{state}")

                self.assertTrue(answer["ok"], answer)
                review = ManagedResource.objects.get(kind=KIND)
                self.assertEqual(review.spec, {**ASKED, "state": state})
                claimed = claim_next_operation("controller-example", capabilities=((KIND, "reconcile"),))
                operation = OperationRequest.objects.get(pk=claimed["operation"]["id"])
                self.assertEqual((operation.resource, operation.requested_actor), (review, PERSON.actor))
                OperationRequest.objects.all().delete()

    def test_the_press_posts_through_its_route(self):
        from hq.platform.core.testing import operator

        operator(self.client)
        url = reverse(
            "control_plane:deploy_reject",
            kwargs={"owner": "example", "name": "alpha", "run_id": 41, "environment": "release", "head_sha": HEAD},
        )

        self.assertEqual(self.client.post(url, {"__retry_key": "example-press"}).status_code, 302)
        self.assertEqual(ManagedResource.objects.get(kind=KIND).spec["state"], "rejected")
        self.assertTrue(OperationRequest.objects.filter(action=OperationRequest.Action.RECONCILE).exists())

    def test_an_agents_answer_waits_for_a_person(self):
        answer = self.press(principal=AGENT)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(ManagedResource.objects.filter(kind=KIND).exists())
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_principal_who_may_not_change_infrastructure_is_refused(self):
        reader = Principal("example-reader", "web", frozenset({Capability.READ}))

        answer = self.press(principal=reader)

        self.assertFalse(answer["ok"], answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_what_is_not_waiting_there_at_that_commit_is_refused(self):
        refused = {
            "a commit that is not whole": {"head_sha": "abc"},
            "another commit": {"head_sha": "e" * 40},
            "another environment": {"environment": "staging"},
            "another run": {"run_id": 42},
        }
        for case, changes in refused.items():
            with self.subTest(case=case):
                answer = self.press(key=f"example-{case}", **changes)

                self.assertFalse(answer["ok"], answer)
                self.assertFalse(OperationRequest.objects.exists())

    def test_an_environment_hq_is_not_a_rule_on_is_answered_on_github(self):
        store(waiting=[RUN], environments=[{**RELEASE, "hq_rule": False}])

        self.assertFalse(self.press()["ok"])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_commit_production_already_runs_is_rejected_never_approved(self):
        store(
            waiting=[RUN],
            environments=[RELEASE],
            deployments=[{"environment": "production", "sha": HEAD, "state": "success"}],
        )

        self.assertFalse(self.press()["ok"])
        self.assertFalse(OperationRequest.objects.exists())
        self.assertTrue(self.press(REJECT, key="example-reject")["ok"])
