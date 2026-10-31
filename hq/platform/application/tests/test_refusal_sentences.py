"""A refusal says what HQ wrote, and nothing the caller sent or a dependency said.

Each case is a refusal whose sentence HQ writes itself rather than passing on
a record's key, a provider's name, a parse error or a held request's own words,
answered through the capability runner as every adapter answers it.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource

from ..adoption import ADOPT, ADOPT_SERVICE, unmanaged
from ..adoption_testing import connection, managing_everything
from ..approvals import APPROVE
from ..capabilities import execute_capability
from ..certificates import material_for
from ..record_errors import CertificateError
from ..security import cli_principal, mcp_principal, web_principal
from .test_adoption import ZONE_KIND, swept
from .test_approvals import POLICY_KEY, an_operator, declare_policy, policy_document, update_payload

# A key HQ holds that a refusal used to name, and must no longer.
KEY = "example-sent-key"


class RefusalCase(TestCase):
    """A tailnet policy is a gated kind, so a change to it is asked as a signed-in person, who is not held."""

    def refused(self, name, payload, *, target=None, principal=None):
        result = execute_capability(name, payload, principal=principal or cli_principal(), target=target)
        self.assertFalse(result["ok"], result)
        return result["error"]


def _operation():
    return {"idempotency_key": "once", "reason": "Stated reason for the change."}


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class DeclarationRefusalTests(RefusalCase):
    def setUp(self):
        managing_everything()

    def test_a_record_switched_off_is_refused_without_its_key(self):
        ManagedResource.objects.create(
            key=KEY, kind="adguard.rewrite", spec={"domain": "a.example.com", "answer": "192.0.2.1"}, enabled=False
        )

        error = self.refused("infrastructure.reconcile", _operation(), target=KEY)

        self.assertEqual(error["code"], "precondition")
        self.assertIn("This record is switched off in HQ.", error["message"])
        self.assertNotIn(KEY, error["message"])

    def test_a_drifted_record_is_refused_without_its_key(self):
        policy = declare_policy()
        policy.conditions = [{"type": "Drifted", "status": True, "reason": "Drifted", "message": "differs"}]
        policy.save(update_fields=["conditions"])

        error = self.refused(
            "tailnet.policy.remove_empty_groups", _operation(), target=POLICY_KEY, principal=an_operator()
        )

        self.assertEqual(error["code"], "precondition")
        self.assertIn("This record differs from what is live.", error["message"])
        self.assertNotIn(POLICY_KEY, error["message"])

    def test_an_action_the_controller_does_not_take_is_said_in_hq_words(self):
        ManagedResource.objects.create(
            key=KEY, kind="adguard.rewrite", spec={"domain": "a.example.com", "answer": "192.0.2.1"}
        )

        error = self.refused("infrastructure.restart", _operation(), target=KEY)

        self.assertEqual(error["code"], "precondition")
        self.assertIn("The controller does not do that to this kind of record.", error["message"])
        self.assertNotIn("adguard.rewrite", error["message"])


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class PolicyAmendmentRefusalTests(RefusalCase):
    """The policy is the tailnet's document: neither its parse error nor its names are HQ's words."""

    def setUp(self):
        managing_everything()
        declare_policy()

    def stored(self, document: str) -> None:
        ManagedResource.objects.filter(key=POLICY_KEY).update(spec={"document": document})

    def test_a_document_that_is_not_json_is_refused(self):
        self.stored('{"groups": example-unparsed')

        error = self.refused(
            "tailnet.policy.remove_empty_groups", _operation(), target=POLICY_KEY, principal=an_operator()
        )

        self.assertEqual(error["code"], "precondition")
        self.assertIn("HQ cannot read the policy as a JSON object", error["message"])
        self.assertNotIn("example-unparsed", error["message"])
        self.assertNotIn("line 1", error["message"])

    def test_a_group_named_outside_its_rules_is_refused_without_its_names(self):
        self.stored(
            json.dumps(
                {
                    "groups": {"group:example-empty": []},
                    "grants": [{"src": ["group:example-empty"], "dst": ["tag:example"], "ip": ["*"]}],
                    "tagOwners": {"tag:example": ["group:example-empty"]},
                }
            )
        )

        error = self.refused(
            "tailnet.policy.remove_empty_groups", _operation(), target=POLICY_KEY, principal=an_operator()
        )

        self.assertEqual(error["code"], "precondition")
        self.assertIn("A group the policy grants is also named outside its rules", error["message"])
        self.assertNotIn("group:example-empty", error["message"])
        self.assertNotIn("tagOwners", error["message"])


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ReadOnlyAdoptionRefusalTests(RefusalCase):
    def setUp(self):
        connection("cloudflare_dns", "example-dns", manages=False)
        swept()

    def test_a_name_read_only_through_read_only_connections_is_refused_without_it(self):
        error = self.refused(ADOPT_SERVICE, {"hostname": "www.example.com"})

        self.assertEqual(error["code"], "precondition")
        self.assertIn("HQ reads that name only through connections that only read.", error["message"])
        self.assertNotIn("www.example.com", error["message"])

    def test_a_record_read_through_a_read_only_connection_is_refused_in_hq_words(self):
        (zone,) = [item for item in unmanaged() if item.kind == ZONE_KIND]

        error = self.refused(ADOPT, {"kind": ZONE_KIND, "token": zone.token})

        self.assertEqual(error["code"], "precondition")
        self.assertIn("This record is read through a connection that only reads.", error["message"])
        self.assertFalse(ManagedResource.objects.exists())


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class ApprovalRefusalTests(RefusalCase):
    def test_a_held_request_for_something_gone_is_refused_without_its_name(self):
        managing_everything()
        declare_policy()
        execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:elsewhere")),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        held = ApprovalRequest.objects.get()
        ApprovalRequest.objects.filter(pk=held.pk).update(capability="example.gone")
        user = get_user_model().objects.create_user(username="reviewer", password="test-only-password")

        error = self.refused(APPROVE, {"approval_id": str(held.pk)}, principal=web_principal(user))

        self.assertEqual(error["code"], "precondition")
        self.assertIn("What this request asks for is no longer something HQ can do", error["message"])
        self.assertNotIn("example.gone", error["message"])


class CertificateMaterialRefusalTests(TestCase):
    """The controller asks for material by key over the bridge, not as a capability."""

    def test_a_key_with_no_stored_certificate_is_not_named(self):
        with self.assertRaises(CertificateError) as refused:
            material_for(KEY)

        self.assertEqual(refused.exception.reason, "No certificate is stored for that record.")
