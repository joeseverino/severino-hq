"""A capability and the button that runs it say the same words (Design, the
one-name table): a capability whose button has a word of its own is named
starting with that word, so "Edit" opens what the audit log calls "Edit an asset"."""

from django.test import SimpleTestCase

from ..capabilities import capability_registry

# Each capability whose button leaves the label's words, and why that reads right.
EXEMPT = {
    "certificate.renew": "its button is the operation's word (OperationRequest.Action.RENEW); the name adds the article",
    "tailnet.routes.approve": (
        "its button is the operation's word (OperationRequest.Action.APPROVE_ROUTES); the name says which routes"
    ),
    "dashboard.machine.select": "its button is on the machine's page, the 'on' of a switch, so it leaves the machine out",
    "infrastructure.resource.adopt": "its button stands beside the record it acts on, so it leaves the record out",
    "tailnet.reach.allow": "its button is on the certificate's page and says where the change lands",
}


class CapabilityVerbTests(SimpleTestCase):
    def test_a_capability_is_named_starting_with_its_button_s_word(self):
        registry = capability_registry()
        for name, spec in registry.items():
            if not spec.verb or name in EXEMPT:
                continue
            with self.subTest(capability=name):
                self.assertTrue(spec.title.startswith(spec.verb), f"{spec.title!r} does not start with {spec.verb!r}")

    def test_every_exemption_is_a_capability_with_a_button_word_of_its_own(self):
        registry = capability_registry()
        for name in EXEMPT:
            with self.subTest(capability=name):
                self.assertIn(name, registry)
                self.assertTrue(registry[name].verb)
                self.assertFalse(registry[name].title.startswith(registry[name].verb), "no longer needs its exemption")
