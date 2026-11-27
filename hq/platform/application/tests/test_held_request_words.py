"""A held request reads as words wherever HQ shows it, never as a payload dump."""

from typing import Any

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest

from ..approval_review import asked, preview
from ..approvals import DECLARATIONS
from ..attention import _asked_change
from ..capabilities import capability_registry, command_spec
from ..integration_specs import capability_schema
from ..projects import DEPENDENCY_UPDATES

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

    def test_a_switch_is_said_as_the_side_it_sets(self):
        spec = command_spec(DEPENDENCY_UPDATES)
        for value, variant in ((True, "on"), (False, "off")):
            with self.subTest(value=value):
                said = str(asked(self.held(DEPENDENCY_UPDATES, {"dependency_updates": value})))

                self.assertIn(spec.words(variant).lower(), said)


class EveryCapabilityPreviewTests(TestCase):
    """A guard over the real registry: whatever a capability takes, its held
    request is described in words, created, changed or deleted."""

    def test_every_record_capability_previews_in_words(self):
        for spec in capability_registry():
            if spec.subject_resource == DECLARATIONS:
                continue  # a declaration is compared by path, as a document
            payload = {field: _sample(schema) for field, schema in capability_schema(spec)["properties"].items()}
            # A deletion is shown by what the record holds now.
            baseline = payload if spec.effect == "destructive" else {}
            for target in ("", "example"):
                shown = preview(
                    ApprovalRequest(capability=spec.name, target=target, payload=payload, baseline=baseline)
                )
                said = [_asked_change(shown)] + [part for row in shown.rows for part in (row.before, row.after)]
                with self.subTest(capability=spec.name, target=target):
                    self.assertEqual(dumped(" ".join(said)), [], said)
