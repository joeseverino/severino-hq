"""When a declaration and what a provider holds differ, and how HQ says so."""

from django.test import TestCase

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.providers import validate_spec
from hq.platform.core.testing import declare

from ..record_errors import RecordNotFound
from ..security import cli_principal


class LineEndingsAreNotDriftTests(TestCase):
    """A document saved through a form is the same document the API returns.

    HTML submits a textarea as CRLF and every provider returns LF, so the two
    differ byte for byte while saying exactly the same thing. Compared raw, a
    policy reads as drifted seconds after a reconcile the provider accepted.
    """

    DOCUMENT = '{\n  "grants": [],\n  "groups": {}\n}'

    def test_a_spec_saved_with_crlf_is_stored_with_one_newline(self):
        """Normalized on the way in as well, so the stored value is comparable
        to anything, not only to what this one comparison normalizes.
        """

        spec = validate_spec(
            "tailscale.policy",
            {"connection_ref": "", "document": self.DOCUMENT.replace("\n", "\r\n")},
        )

        self.assertEqual(spec["document"], self.DOCUMENT)


class AcceptObservedTests(TestCase):
    """Keeping a change made at the provider, instead of reconciling it away."""

    def setUp(self):
        from ..inventory_testing import store

        self.resource = declare(
            "a-device", "tailscale.device", {"connection_ref": "", "name": "a-box", "key_expiry_disabled": False}
        )
        # The provider says the key never expires: it was changed there.
        store("tailscale.device", {"name": "a-box", "key_expires": "", "tags": []})

    def accept(self):
        from ..resource_operations import OperationCommand, accept_observed
        from .consent import consented

        return accept_observed(
            OperationCommand(reason="changed on purpose"),
            consented=consented(cli_principal()),
            current_key="a-device",
        )

    def test_nothing_seen_is_nothing_to_accept(self):
        ProviderInventory.objects.filter(kind="tailscale.device").delete()

        with self.assertRaises(RecordNotFound):
            self.accept()


class DriftPhraseTests(TestCase):
    """A drift is said so a person can see what changed, not pasted whole."""

    def test_a_long_value_that_is_not_a_document_is_not_pasted(self):
        from ..inventory import _difference_phrase

        phrase = _difference_phrase("note", "a" * 200, "b" * 300)

        self.assertNotIn("bbbb", phrase)
        self.assertIn("note", phrase)
