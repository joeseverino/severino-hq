"""A refusal says what HQ wrote, and nothing the caller sent or a dependency said.

Each case is a refusal whose sentence HQ writes itself rather than passing on
a record's key, a provider's name, a parse error or a held request's own words,
answered through the capability runner as every adapter answers it.
"""

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.testing import connection, managing_everything

from ..adoption import ADOPT_SERVICE
from ..capabilities import execute_capability
from ..certificates import material_for
from ..record_errors import CertificateError
from ..security import cli_principal
from .test_adoption import swept
from .test_approvals import POLICY_KEY, an_operator, declare_policy
from .writers import keyed

# A key HQ holds that a refusal used to name, and must no longer.
KEY = "example-sent-key"


class RefusalCase(TestCase):
    """A tailnet policy is a gated kind, so a change to it is asked as a signed-in person, who is not held."""

    def refused(self, name, payload, *, target=None, principal=None):
        result = execute_capability(name, keyed(payload), principal=principal or cli_principal(), target=target)
        self.assertFalse(result["ok"], result)
        return result["error"]


def _operation():
    return {"idempotency_key": "once", "reason": "Stated reason for the change."}


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
        self.assertNotIn("example-unparsed", error["message"])
        self.assertNotIn("line 1", error["message"])


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class ReadOnlyAdoptionRefusalTests(RefusalCase):
    def setUp(self):
        connection("cloudflare_dns", "example-dns", manages=False)
        swept()

    def test_a_name_read_only_through_read_only_connections_is_refused_without_it(self):
        error = self.refused(ADOPT_SERVICE, {"hostname": "www.example.com"})

        self.assertEqual(error["code"], "precondition")
        self.assertNotIn("www.example.com", error["message"])


class CertificateMaterialRefusalTests(TestCase):
    """The controller asks for material by key over the bridge, not as a capability."""

    def test_a_key_with_no_stored_certificate_is_not_named(self):
        with self.assertRaises(CertificateError) as refused:
            material_for(KEY)

        self.assertNotIn(KEY, refused.exception.reason)
