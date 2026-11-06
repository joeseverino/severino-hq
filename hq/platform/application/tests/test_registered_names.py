"""Every capability and resource name HQ registers, the host's and each
extension's, follows the API's naming pattern (docs/API.md#names).

The registry read is the composed one, so the composed suite holds every
installed extension's names to it as well as the host's. The pattern itself
is held by properties, which Hypothesis (a development tool the image lacks)
writes names for.
"""

import re
from unittest import skipUnless

from django.test import SimpleTestCase

try:
    from hypothesis import example, given, strategies as st
except ImportError:  # pragma: no cover - the image does not carry it
    HYPOTHESIS = False
else:
    HYPOTHESIS = True

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


if HYPOTHESIS:
    WORD = st.from_regex(r"[a-z][a-z0-9]{0,5}(?:_[a-z0-9]{1,4}){0,2}", fullmatch=True)
    NOUN = WORD.filter(lambda word: word not in VERBS and word not in STUTTER)
    CAPABILITY_NAME = st.builds(
        lambda path, verb: ".".join([*path, verb]),
        st.lists(WORD, min_size=1, max_size=3),
        st.sampled_from(sorted(VERBS)),
    )
    RESOURCE_NAME = st.builds(lambda owner, noun: ".".join([*owner, noun]), st.lists(WORD, max_size=1), NOUN)

    def _capitalized(name: str) -> str:
        return name[0].upper() + name[1:]

    # Each way a name goes wrong, from a name that was right.
    OFF_CAPABILITY = st.one_of(
        CAPABILITY_NAME.map(_capitalized),
        CAPABILITY_NAME.map(lambda name: name.replace(".", "-")),
        CAPABILITY_NAME.map(lambda name: name.rpartition(".")[2]),
        st.builds(lambda name, noun: f"{name.rpartition('.')[0]}.{noun}", CAPABILITY_NAME, NOUN),
    )
    OFF_RESOURCE = st.one_of(
        RESOURCE_NAME.map(_capitalized),
        RESOURCE_NAME.map(lambda name: name.replace(".", "-") + "-x"),
        st.builds(lambda owner, stutter: f"{owner}.{stutter}", WORD, st.sampled_from(sorted(STUTTER))),
        st.builds(lambda owner, name: f"{owner}.{name}.{owner}", WORD, RESOURCE_NAME),
    )


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class NamePatternProperties(SimpleTestCase):
    """The pattern the registry is held to passes every name it describes, and
    refuses every other by the pattern and the name."""

    if HYPOTHESIS:

        @given(CAPABILITY_NAME)
        @example("example.item.set_aside")
        def test_a_capability_name_on_the_pattern_passes(self, name):
            self.assertIsNone(capability_refusal(name))

        @given(RESOURCE_NAME)
        @example("entries")
        def test_a_resource_name_on_the_pattern_passes(self, name):
            self.assertIsNone(resource_refusal(name))

        @given(OFF_CAPABILITY)
        @example("lookup.address")
        @example("example.ledger.decide_entry")
        def test_a_capability_name_off_it_is_refused_by_the_pattern_and_its_name(self, name):
            why = capability_refusal(name) or ""

            self.assertIn(CAPABILITY_PATTERN, why)
            self.assertIn(repr(name), why)

        @given(OFF_RESOURCE)
        @example("infrastructure.resources")
        @example("example.ledger.entries")
        def test_a_resource_name_off_it_is_refused_by_the_pattern_and_its_name(self, name):
            why = resource_refusal(name) or ""

            self.assertIn(RESOURCE_PATTERN, why)
            self.assertIn(repr(name), why)
