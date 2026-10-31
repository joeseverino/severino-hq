"""A held request reads as words wherever HQ shows it, never as a payload dump."""

from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule
from hq.platform.core.models import AuditLog

from ..approvals import AUDIT_LABELS, DECLARATIONS, asked, preview
from ..attention import _asked_change, waiting_for_approval
from ..capabilities import capability_registry, execute_capability
from ..integration_specs import capability_schema
from ..projects import DEPENDENCY_UPDATES
from ..security import Capability, Principal
from .markup import select, text

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
        self.assertIn("manifest set to 4 entries", item.body)
        self.assertIn("confirm prune set to yes", item.body)

    def test_a_command_on_no_record_is_not_said_to_create_one(self):
        (item,) = waiting_for_approval()

        self.assertNotIn("created", item.body.lower(), item.body)

    def test_the_audit_says_it_as_the_card_does(self):
        event = AuditLog.objects.get(object_type__in=AUDIT_LABELS, object_id=str(self.held.id))

        self.assertEqual(event.message, str(asked(self.held)))

    def test_the_decision_card_says_it_in_words(self):
        user = get_user_model().objects.create_user(username="reviewer", password="test-only-password")
        self.client.force_login(user)

        entry = self.client.get(reverse("core:approval_entry", kwargs={"approval_id": self.held.id}), follow=True)

        shown = text(entry)
        self.assertIn("4 entries", shown)
        self.assertNotIn("{'doc_id'", shown)
        self.assertNotIn("True", shown)

    def test_its_change_is_held_whole_beside_its_buttons(self):
        """The dashboard's card and the queue's row mark what Approve decides
        as never cut (the browser gate's what_a_button_decides_is_shown_whole
        holds the mark to its layout)."""

        user = get_user_model().objects.create_superuser("reviewer", "reviewer@example.test", "x" * 20)
        self.client.force_login(user)
        (item,) = waiting_for_approval()

        for page in (reverse("dashboard"), reverse("action_items")):
            with self.subTest(page=page):
                details = [element.words for element in select(self.client.get(page), "data-whole")]
                self.assertIn(" ".join(item.body.split()), details)


class HeldSentenceTests(TestCase):
    """What a held request wants done, said from what its command declares."""

    def held(self, capability: str, payload: dict[str, Any], baseline: dict[str, Any] | None = None):
        return ApprovalRequest(
            capability=capability,
            target="example-dns",
            payload=payload,
            baseline=baseline or {},
            requested_actor="example-agent",
        )

    def test_a_change_of_one_value_is_said_as_that_change(self):
        before = {"spec": {"answer": "192.0.2.1", "domain": "example.test"}}
        moved = self.held(
            "infrastructure.resource.update", {"spec": {"answer": "192.0.2.2", "domain": "example.test"}}, before
        )
        both = self.held(
            "infrastructure.resource.update", {"spec": {"answer": "192.0.2.2", "domain": "example.org"}}, before
        )

        said = asked(moved)
        self.assertTrue(said.whole)
        self.assertEqual(said.target, "example-dns")
        self.assertTrue(str(said).endswith("example-dns from 192.0.2.1 to 192.0.2.2"), said)
        # Two values are left to the table beneath it.
        self.assertFalse(asked(both).whole)
        self.assertNotIn("192.0.2.2", str(asked(both)))

    def test_a_record_is_named_as_a_person_knows_it_not_by_its_key(self):
        before = {"spec": {"answer": "192.0.2.1", "domain": "app.example.test"}}
        held = self.held(
            "infrastructure.resource.update", {"spec": {"answer": "192.0.2.2", "domain": "app.example.test"}}, before
        )
        held.resource_kind = "adguard.rewrite"

        said = asked(held)
        self.assertEqual(said.target, "app.example.test")
        self.assertNotIn("example-dns", str(said))

    def test_a_switch_is_said_as_the_side_it_sets(self):
        spec = capability_registry()[DEPENDENCY_UPDATES]
        for value, variant in ((True, "on"), (False, "off")):
            with self.subTest(value=value):
                said = str(asked(self.held(DEPENDENCY_UPDATES, {"dependency_updates": value})))

                self.assertIn(spec.words(variant).lower(), said)


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
