"""A command form posts one retry key, and a resend under it acts once."""

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.tests.writers import drawn_key
from hq.platform.application.web_writes import RETRY_KEY_FIELD
from hq.platform.core.testing import declare, operator

RESOURCE = "example-rewrite"


class KeptKeyTests(TestCase):
    def setUp(self):
        managing_everything()
        declare(RESOURCE, "adguard.rewrite", {"domain": "app.example.com", "answer": "192.0.2.10"})
        operator(self.client)

    def test_a_resend_under_the_form_s_key_queues_nothing_more(self):
        opened = self.client.get("/commands/infrastructure.resource.reconcile/", {"target": RESOURCE})
        form = opened.context["form"]
        key = drawn_key(opened, "data-command-form")
        posted = {
            name: value
            for name, value in {
                **form.initial,
                "__target": RESOURCE,
                "__confirm_effect": "on",
                RETRY_KEY_FIELD: key,
            }.items()
            if value is not None
        }

        response = self.client.post("/commands/infrastructure.resource.reconcile/", posted)
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        # Done, so only the key, and no waiting operation, keeps a resend from queueing another.
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at=timezone.now())

        self.client.post("/commands/infrastructure.resource.reconcile/", posted)

        self.assertEqual(OperationRequest.objects.count(), 1)
