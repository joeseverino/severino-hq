"""A held tailnet policy change says first what it opens and which deny tests it drops."""

import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.platform.application.approval_review import preview
from hq.platform.application.attention import _asked_change

LIVE = {
    "grants": [{"src": ["example-laptop"], "dst": ["example-server"], "ip": ["443"]}],
    "tests": [{"src": "example-laptop", "accept": ["example-server:443"], "deny": ["example-server:22"]}],
}
OPENED = {
    "grants": [
        {"src": ["example-laptop"], "dst": ["example-server"], "ip": ["443"]},
        {"src": ["example-laptop"], "dst": ["example-server"], "ip": ["22", "tcp:3389"]},
    ],
    "tests": [{"src": "example-laptop", "accept": ["example-server:443"]}],
}


def held(before: dict, after: dict) -> SimpleNamespace:
    return SimpleNamespace(
        capability="infrastructure.resource.update",
        resource_kind="tailscale.policy",
        baseline={"spec": {"document": json.dumps(before)}},
        payload={"spec": {"document": json.dumps(after)}},
    )


class PolicyChangeWordsTests(SimpleTestCase):
    def test_a_change_that_opens_access_and_drops_a_deny_test_says_both_first(self):
        shown = preview(held(LIVE, OPENED))

        heading = shown.label
        self.assertIn("example-server:22, tcp:3389", heading)
        self.assertIn("example-laptop", heading)
        self.assertIn("denied example-server:22", heading)
        self.assertTrue(heading.startswith("Opens"))
        # The detail stays beneath it.
        self.assertTrue(shown.changes)

    def test_the_needs_you_row_reads_the_same_words(self):
        self.assertTrue(_asked_change(preview(held(LIVE, OPENED))).startswith(preview(held(LIVE, OPENED)).label))

    def test_a_change_that_only_narrows_keeps_the_plain_heading(self):
        narrowed = {**LIVE, "grants": []}

        self.assertEqual(preview(held(LIVE, narrowed)).label, "Would change")
