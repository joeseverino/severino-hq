"""A review sets what it was sent and keeps the rest of the message's review,
as every update does: it needs only the message it reviews."""

from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.cloudflare import ContactReview
from hq.platform.application.capabilities import command_spec, execute_capability
from hq.platform.application.contact_submissions import ASSIGNEE_LENGTH, REVIEW
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.json_schemas import fields
from hq.platform.application.tests.writers import keyed
from hq.platform.core.testing import connection

from .. import inbox
from .test_contacts import OPERATOR, REF, _read, _row


def _reviews():
    return ManagedResource.objects.get(kind=inbox.DECLARATION).spec["reviews"]


class AReviewKeepsWhatItWasNotSentTests(TestCase):
    def setUp(self):
        _read(_row(2, status="read", assigned_to="example-assignee", admin_notes="Kept."))
        connection("cloudflare_api", REF, manages=True)

    def test_a_review_sent_only_its_status_keeps_who_has_it_and_its_notes(self):
        result = execute_capability(REVIEW, keyed({"status": "replied"}), principal=OPERATOR, target=2)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            _reviews(), [{"id": 2, "status": "replied", "assigned_to": "example-assignee", "admin_notes": "Kept."}]
        )

    def test_a_review_sent_only_a_note_keeps_its_status_and_who_has_it(self):
        result = execute_capability(REVIEW, keyed({"admin_notes": "Answered."}), principal=OPERATOR, target=2)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            _reviews(), [{"id": 2, "status": "read", "assigned_to": "example-assignee", "admin_notes": "Answered."}]
        )

    def test_a_review_of_a_message_that_is_not_there_is_refused_as_not_found(self):
        result = execute_capability(REVIEW, keyed({"status": "replied"}), principal=OPERATOR, target=99)

        self.assertEqual(result["error"]["code"], "not_found")
        self.assertFalse(ManagedResource.objects.exists())


class TheReviewSchemaTests(SimpleTestCase):
    def test_a_review_requires_nothing_and_says_how_long_its_assignee_and_notes_may_be(self):
        schema = capability_schema(command_spec(REVIEW))
        applied = ContactReview.model_json_schema()["properties"]

        self.assertNotIn("required", schema)
        self.assertEqual(fields(schema)["assigned_to"]["maxLength"], ASSIGNEE_LENGTH)
        self.assertEqual(fields(schema)["admin_notes"].get("maxLength"), applied["admin_notes"]["maxLength"])
