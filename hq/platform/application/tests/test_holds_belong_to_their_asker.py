"""A held change belongs to the caller that asked it.

Two callers asking the same change hold two requests, each read and approved
as its own asker; the change still applies once, because the second approval
finds what it was measured against already moved. A credential reads only its
own held requests, wherever they are listed to it; a person reads every one.
"""

from unittest.mock import patch

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest

from ..approval_review import asked
from ..capabilities import execute_capability
from ..security import Capability, Principal
from .test_approvals import POLICY_KEY, declare_policy, reconcile_payload

GRANT = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})
FIRST = Principal("example-agent", "mcp", GRANT)


def ask_reconcile(principal: Principal) -> dict:
    return execute_capability(
        "infrastructure.resource.reconcile", reconcile_payload(), principal=principal, target=POLICY_KEY
    )


class HoldsBelongToTheirAskerTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare_policy()

    def test_a_same_caller_ask_written_between_the_read_and_the_write_answers_the_existing_request(self):
        written = []

        def the_other_ask_commits_first(held, *args, **kwargs):
            # The identical ask from the same caller, landing after this one
            # looked and found nothing, and before it inserts.
            if not written:
                twin = ApprovalRequest(
                    **{
                        field.attname: getattr(held, field.attname)
                        for field in ApprovalRequest._meta.concrete_fields
                        if not field.primary_key
                    }
                )
                twin.save(force_insert=True)
                written.append(twin)
            return asked(held, *args, **kwargs)

        with patch("hq.platform.application.approvals.asked", side_effect=the_other_ask_commits_first):
            answered = ask_reconcile(FIRST)

        self.assertTrue(answered["ok"])
        self.assertTrue(answered["repeated"])
        self.assertEqual(answered["approval"]["id"], str(written[0].pk))
        self.assertEqual(ApprovalRequest.objects.count(), 1)
