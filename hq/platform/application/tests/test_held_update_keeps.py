"""A held update needs only its target too: it is held as sent, and applied
once agreed to with every field it was not sent kept."""

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource

from ..approvals import ApproveCommand, approve
from ..capabilities import execute_capability
from ..security import mcp_principal
from .test_approvals import GATED_KIND, POLICY_KEY, an_operator, declare_policy, policy_document
from .writers import keyed


class AHeldUpdateKeepsTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()

    def test_an_update_sent_only_its_spec_is_held_as_sent_and_applied_keeping_its_key_and_kind(self):
        changed = {"document": policy_document("group:elsewhere")}

        answer = execute_capability(
            "infrastructure.resource.update",
            keyed({"spec": changed}),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        held = ApprovalRequest.objects.get()
        self.assertNotIn("kind", held.payload)
        approve(ApproveCommand(str(held.id)), principal=an_operator())
        resource = ManagedResource.objects.get(key=POLICY_KEY)
        self.assertEqual(
            (resource.kind, resource.spec["document"], resource.enabled), (GATED_KIND, changed["document"], True)
        )
