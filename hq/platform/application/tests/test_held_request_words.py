"""A held request reads as words wherever HQ shows it, never as a payload dump."""

from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule

from ..approvals import DECLARATIONS, preview
from ..attention import _asked_change, waiting_for_approval
from ..capabilities import capability_registry, execute_capability
from ..integration_specs import capability_schema
from ..security import Capability, Principal
from .markup import text

# What a Python dump of a dict or a list leaves in the text.
DUMPED = ("{", "[", "'")


def dumped(words: str) -> list[str]:
    return [mark for mark in DUMPED if mark in words]


def _sample(schema: dict[str, Any]) -> Any:
    """A value of each JSON type a payload can carry, nested where it may be."""

    for option in (schema, *schema.get("anyOf", ())):
        if option.get("enum"):
            return option["enum"][0]
        kind = option.get("type")
        if kind == "boolean":
            return True
        if kind in ("integer", "number"):
            return 2
        if kind == "string":
            return "example"
        if kind == "array":
            item = _sample(option.get("items") or {})
            return [item, item]
    return {"name": "example", "nested": {"value": 1}}


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class HeldRequestWordsTests(TestCase):
    def setUp(self):
        CapabilityRule.objects.create(scope="surface", subject="mcp", capability="documentation.sync", rule="approve")
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.SYNC_DOCUMENTATION}))
        held = execute_capability(
            "documentation.sync",
            {
                "manifest": [{"doc_id": f"rb-example-{index}", "title": "Example"} for index in range(4)],
                "prune_orphans": True,
                "confirm_prune": True,
            },
            principal=agent,
        )
        self.held = ApprovalRequest.objects.get(pk=held["approval"]["id"])

    def test_needs_you_says_what_a_list_and_a_flag_mean(self):
        (item,) = waiting_for_approval()

        self.assertEqual(dumped(item.body), [], item.body)
        self.assertIn("manifest to 4 entries", item.body)
        self.assertIn("confirm prune to yes", item.body)

    def test_the_decision_card_says_it_in_words(self):
        user = get_user_model().objects.create_user(username="reviewer", password="test-only-password")
        self.client.force_login(user)

        entry = self.client.get(reverse("core:approval_entry", kwargs={"approval_id": self.held.id}), follow=True)

        shown = text(entry)
        self.assertIn("4 entries", shown)
        self.assertNotIn("{'doc_id'", shown)
        self.assertNotIn("True", shown)


class EveryCapabilityPreviewTests(TestCase):
    """A guard over the real registry: whatever a capability takes, its held
    request is described in words, created, changed or deleted."""

    def test_every_record_capability_previews_in_words(self):
        for name, spec in capability_registry().items():
            if spec.subject_resource == DECLARATIONS:
                continue  # a declaration is compared by path, as a document
            payload = {field: _sample(schema) for field, schema in capability_schema(spec)["properties"].items()}
            # A deletion is shown by what the record holds now.
            baseline = payload if spec.effect == "destructive" else {}
            for target in ("", "example"):
                shown = preview(ApprovalRequest(capability=name, target=target, payload=payload, baseline=baseline))
                said = [_asked_change(shown)] + [part for row in shown.rows for part in (row.before, row.after)]
                with self.subTest(capability=name, target=target):
                    self.assertEqual(dumped(" ".join(said)), [], said)
