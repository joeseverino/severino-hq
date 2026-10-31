"""The capability catalog lists only what its asker's interface could ever hold."""

from dataclasses import replace

from django.test import SimpleTestCase

from hq_sdk.capabilities import StrictCommand

from ..capabilities import capability_registry, describe_capabilities
from ..capability_policy import SURFACES, agents_may_hold
from ..integration_specs import CapabilitySpec
from ..plugin_testing import installed, sibling
from ..security import HELD_ONLY_AT, AuthorizationError, Capability, Principal, could_hold, operator_capabilities

PERSON_ONLY = ("certificate.upload", "receipt.upload")


class _Nothing(StrictCommand):
    pass


def _an_extension():
    """An extension with one command of its own, behind a grant of its own, so
    the host alone has a capability no host grant covers."""

    manifest, integration = sibling(
        identifier="example.catalog",
        name="Catalog",
        operator_capabilities=("example.catalog.write",),
    )
    command = CapabilitySpec(
        "example.catalog.run",
        "Run the example.",
        "remote_write",
        "example.catalog.write",
        _Nothing,
        lambda command, *, principal: {"ok": True},
    )
    return manifest, replace(integration, capabilities=lambda: (command,))


def _as(interface: str) -> Principal:
    # Every capability granted, extensions' included, so only the interface
    # can keep one out.
    return Principal(f"example-{interface}", interface, operator_capabilities())


def _listed(principal: Principal) -> set[str]:
    return {item["name"] for item in describe_capabilities(principal=principal)["capabilities"]}


class CatalogTests(SimpleTestCase):
    def test_a_token_is_never_shown_what_only_an_operator_holds(self):
        for interface in ("api", "mcp"):
            with self.subTest(interface=interface):
                listed = _listed(_as(interface))
                self.assertTrue(listed)
                for name in PERSON_ONLY:
                    self.assertNotIn(name, listed)

    def test_the_operator_sees_the_uploads(self):
        for interface in ("web", "cli"):
            with self.subTest(interface=interface):
                listed = _listed(_as(interface))
                for name in PERSON_ONLY:
                    self.assertIn(name, listed)

    def test_a_capability_lacking_only_a_grant_is_still_listed(self):
        # The catalog tells a token what exists; whether it may run it is the
        # caller's flag to draw.
        bare = Principal("example-api", "api", frozenset())
        self.assertIn("project.create", _listed(bare))
        self.assertFalse(bare.permits(*capability_registry()["project.create"].required_capabilities))

    def test_the_catalog_and_the_runner_agree_on_every_capability(self):
        with installed(_an_extension()):
            self.assertIn("example.catalog.run", capability_registry())
            self._agree_on_every_capability()

    def _agree_on_every_capability(self):
        for interface in ("api", "mcp", "web", "cli"):
            principal = _as(interface)
            listed = _listed(principal)
            for spec in capability_registry().values():
                with self.subTest(interface=interface, capability=spec.name):
                    self.assertEqual(spec.name in listed, principal.permits(*spec.required_capabilities))


class OnePredicateTests(SimpleTestCase):
    def test_require_refuses_a_capability_held_elsewhere_whatever_the_grant(self):
        with self.assertRaisesMessage(AuthorizationError, "held only through"):
            _as("api").require(Capability.UPLOAD_CERTIFICATE)
        _as("cli").require(Capability.UPLOAD_CERTIFICATE)

    def test_a_held_only_capability_is_held_where_named_and_nowhere_new(self):
        for capability, interfaces in HELD_ONLY_AT.items():
            with self.subTest(capability=capability):
                self.assertTrue(all(could_hold(interface, (capability,)) for interface in interfaces))
                # An interface nobody named is a credential until somebody decides otherwise.
                self.assertFalse(could_hold("example-interface", (capability,)))

    def test_the_agent_policy_page_offers_a_rule_only_where_an_agent_catalog_lists_it(self):
        agent_listed = set().union(*(_listed(_as(surface)) for surface in SURFACES))
        for spec in capability_registry().values():
            with self.subTest(capability=spec.name):
                self.assertEqual(agents_may_hold(spec), spec.name in agent_listed)
