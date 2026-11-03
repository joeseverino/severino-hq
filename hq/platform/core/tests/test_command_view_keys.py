"""A command form posts one retry key, and a command that keeps its key keeps that one."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.tests.writers import drawn_key
from hq.platform.application.web_writes import RETRY_KEY_FIELD

DIRECTORY = Path(tempfile.mkdtemp())
RESOURCE = "example-rewrite"


@override_settings(
    SEVERINO_CONTROLLER_DOORBELL=str(DIRECTORY / "doorbell"),
    SEVERINO_ACTIVITY_MARKER=str(DIRECTORY / "activity"),
)
class KeptKeyTests(TestCase):
    def setUp(self):
        managing_everything()
        ManagedResource.objects.create(
            key=RESOURCE,
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "192.0.2.10"},
        )
        user = get_user_model().objects.create_superuser("operator", "operator@example.com", "strongtestpass-1234")
        self.client.force_login(user)

    def test_the_operation_keeps_the_key_the_form_posted(self):
        opened = self.client.get("/commands/infrastructure.reconcile/", {"target": RESOURCE})
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

        response = self.client.post("/commands/infrastructure.reconcile/", posted)

        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        self.assertEqual(OperationRequest.objects.get().idempotency_key, key)
