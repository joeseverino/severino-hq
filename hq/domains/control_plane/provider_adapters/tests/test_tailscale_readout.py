"""The tailnet policy's summary counts what its document holds."""

import json

from django.test import SimpleTestCase

from ..tailscale import _policy_readout


def counts(spec, status):
    return {label: value for label, _hint, value in _policy_readout(spec, status)}


class PolicyReadoutTests(SimpleTestCase):
    DOCUMENT = json.dumps(
        {
            "groups": {"group:admins": ["someone@example.com"], "group:ops": []},
            "grants": [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["*"]}],
            "tests": [{"src": "someone@example.com", "accept": ["tag:server:22"]}],
        }
    )

    def test_the_read_document_is_counted_not_the_status_keys(self):
        # The status stores the document as text: it has no "grants" key.
        self.assertEqual(counts({}, {"document": self.DOCUMENT}), {"Grants": "1", "Groups": "2", "Tests": "1"})

    def test_before_a_read_the_declared_document_is_counted(self):
        self.assertEqual(counts({"document": self.DOCUMENT}, None)["Groups"], "2")

    def test_a_policy_written_as_acls_counts_them_as_grants(self):
        document = json.dumps({"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}] * 3})

        self.assertEqual(counts({"document": document}, {})["Grants"], "3")

    def test_an_unreadable_document_says_nothing_rather_than_zero(self):
        self.assertEqual(counts({"document": "{not json"}, {}), {"Grants": "", "Groups": "", "Tests": ""})
