"""Every capability and resource name HQ registers, the host's and each
extension's, follows the API's naming pattern (docs/API.md#names).

The registry read is the composed one, so the composed suite holds every
installed extension's names to it as well as the host's.
"""

import re

from django.test import SimpleTestCase

from ..capabilities import capability_registry
from ..resources import resource_registry

SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
CAPABILITY = re.compile(rf"^{SEGMENT}(?:\.{SEGMENT})+$")
RESOURCE = re.compile(rf"^(?:{SEGMENT}\.)?{SEGMENT}$")
CAPABILITY_PATTERN = "<area>[.<noun>...].<verb>"
RESOURCE_PATTERN = "[<owner>.]<noun>"

# Every verb a capability's last segment may be. A new one is added here on
# purpose, as a word of the API, rather than coined by a name.
VERBS = frozenset(
    {
        "accept_observed",
        "adopt",
        "allow",
        "approve",
        "choose",
        "close",
        "complete",
        "create",
        "decide",
        "delete",
        "import",
        "list",
        "move",
        "open",
        "read",
        "reconcile",
        "reconnect",
        "refresh",
        "refresh_due",
        "reject",
        "remove",
        "remove_empty_groups",
        "renew",
        "replace",
        "restart",
        "restore",
        "review",
        "run",
        "select",
        "set",
        "set_aside",
        "sign_out",
        "start",
        "stop",
        "sync",
        "toggle",
        "update",
        "upload",
        "upsert",
    }
)

# Every resource is one, and the API serves it under /resources/ already.
STUTTER = frozenset({"resource", "resources"})


def capability_refusal(name: str) -> str | None:
    """Why ``name`` is no capability name, or None for one that is."""

    if not CAPABILITY.match(name):
        return f"{name!r} is not {CAPABILITY_PATTERN} in dotted snake_case."
    verb = name.rpartition(".")[2]
    if verb not in VERBS:
        return f"{name!r} does not end in a verb, as {CAPABILITY_PATTERN} does; {verb!r} is none of VERBS."
    return None


def resource_refusal(name: str) -> str | None:
    """Why ``name`` is no resource name, or None for one that is."""

    if not RESOURCE.match(name):
        return f"{name!r} is not {RESOURCE_PATTERN} in dotted snake_case."
    if name.rpartition(".")[2] in STUTTER:
        return f"{name!r} names its records 'resources', as the path it is served at already says ({RESOURCE_PATTERN})."
    return None


class RegisteredNameTests(SimpleTestCase):
    def test_every_capability_name_follows_the_pattern(self):
        refused = [why for name in capability_registry() if (why := capability_refusal(name))]
        self.assertEqual(refused, [])

    def test_every_resource_name_follows_the_pattern(self):
        refused = [why for name in resource_registry() if (why := resource_refusal(name))]
        self.assertEqual(refused, [])

    def test_a_name_off_the_pattern_is_refused_by_the_pattern_and_its_name(self):
        for name in ("lookup.address", "example.sessions", "example.ledger.decide_entry", "Example.run", "run"):
            with self.subTest(name=name):
                why = capability_refusal(name) or ""
                self.assertIn(CAPABILITY_PATTERN, why)
                self.assertIn(repr(name), why)
        for name in ("infrastructure.resources", "example.ledger.entries", "Entries"):
            with self.subTest(name=name):
                why = resource_refusal(name) or ""
                self.assertIn(RESOURCE_PATTERN, why)
                self.assertIn(repr(name), why)

    def test_a_name_on_the_pattern_passes(self):
        for name in ("example.entry.list", "example.ledger.entry.read", "example.sync", "example.item.set_aside"):
            with self.subTest(name=name):
                self.assertIsNone(capability_refusal(name))
        for name in ("entries", "example.entries"):
            with self.subTest(name=name):
                self.assertIsNone(resource_refusal(name))
