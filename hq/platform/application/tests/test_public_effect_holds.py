"""An agent's change the public internet would see waits for a person, as its delete does.

What counts as public is the provider's ``public_effect``, the host's one
definition of it, so a kind that gains it is held without anything here
naming it.
"""

from itertools import count

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.providers import PROVIDERS, describe_providers

from ..adoption import ADOPT, ADOPT_SERVICE
from ..capabilities import capability_registry, execute_capability
from ..capability_policy import matrix
from ..default_hold import may_be_held_by_default
from ..entity_links import kind_label
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..integration_specs import capability_schema
from ..labels import lower_first
from ..record_errors import PolicyError
from ..security import DEVICE_INTERFACE, Capability, Principal, cli_principal, could_hold
from ..zones import ADOPT_ZONE
from .writers import keyed

COPIES_LIVE = frozenset({ADOPT, ADOPT_SERVICE, ADOPT_ZONE})
EVERYTHING = frozenset(str(capability) for capability in Capability)
_keys = count()


def public_kinds() -> list[str]:
    """The kinds held only for what the public sees, so nothing else explains a hold."""

    return [kind for kind, provider in PROVIDERS.items() if provider.public_effect and not provider.requires_approval]


def access_kind() -> str:
    return next(kind for kind, provider in PROVIDERS.items() if provider.requires_approval)


def fresh_key() -> str:
    return f"example-{next(_keys)}"


def agent(interface: str = "mcp", actor: str = "example-agent") -> Principal:
    """An agent a person signed in, so nothing it asks for is refused for want of one."""

    return Principal(actor, interface, EVERYTHING, granted=EVERYTHING, person_key="example")


def device() -> Principal:
    """An operator's own device, which reaches HQ through the API as a person."""

    return Principal("example-phone", DEVICE_INTERFACE, EVERYTHING, granted=EVERYTHING, person_key="example")


def record(key: str, kind: str) -> ManagedResourceCommand:
    return ManagedResourceCommand(key=key, kind=kind, spec=dict(PROVIDERS[kind].sample_record or {}))


def declare(kind: str) -> ManagedResource:
    key = fresh_key()
    save_managed_resource(record(key, kind), principal=cli_principal(), copied_from_live=True)
    return ManagedResource.objects.get(key=key)


def record_payload(key: str, kind: str) -> dict:
    return {"key": key, "kind": kind, "spec": dict(PROVIDERS[kind].sample_record or {}), "enabled": True}


def least_payload(spec, fields: set[str]) -> dict:
    """A stated reason where the command takes one, and each other field it requires, a choice, chosen."""

    schema = capability_schema(spec)
    payload = {"reason": "Stated reason."} if "reason" in fields else {}
    for name in schema.get("required", ()):
        if name not in payload:
            if schema["properties"][name].get("type") != "boolean":
                raise AssertionError(f"{spec.name} requires {name}, which this test cannot fill in.")
            payload[name] = True
    return payload


def reaching_a_record() -> list[tuple[str, object, set[str]]]:
    """Every capability an agent could run that the default hold reaches and that names a record.

    Read from the registry, so one added later is held without this naming it.
    Adopting is left out: it makes HQ's copy equal what is live.
    """

    reached = []
    for name, spec in capability_registry().items():
        if not may_be_held_by_default(spec) or name in COPIES_LIVE:
            continue
        if not could_hold("mcp", spec.required_capabilities):
            continue
        fields = set(capability_schema(spec).get("properties", {}))
        if spec.target_kind is None and "kind" not in fields:
            continue
        reached.append((name, spec, fields))
    return reached


@override_settings(
    SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True,
)
class HoldTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()

    def test_there_is_a_public_kind_to_hold_and_a_change_that_reaches_it(self):
        self.assertTrue(public_kinds(), "no kind has a public effect, so this hold guards nothing")
        self.assertTrue(reaching_a_record(), "no capability the default hold reaches names a record")

    def test_every_change_an_agent_asks_for_on_a_public_kind_waits(self):
        for kind in public_kinds():
            for name, spec, fields in reaching_a_record():
                with self.subTest(kind=kind, capability=name):
                    resource = declare(kind)
                    target = resource.key if spec.target_kind is not None else None
                    key = resource.key if target else fresh_key()
                    payload = record_payload(key, kind) if "spec" in fields else least_payload(spec, fields)

                    result = execute_capability(name, keyed(payload), principal=agent(actor=key), target=target)

                    self.assertEqual(result.get("status"), "awaiting_approval", result)
                    self.assertEqual(ManagedResource.objects.get(key=resource.key).generation, resource.generation)
                    self.assertFalse(ManagedResource.objects.filter(key=key).exclude(pk=resource.pk).exists())
                    self.assertFalse(OperationRequest.objects.exists())

    def test_an_operators_own_change_to_a_public_kind_runs(self):
        for kind in public_kinds():
            for principal in (device(), cli_principal()):
                with self.subTest(kind=kind, interface=principal.interface):
                    key = fresh_key()

                    result = execute_capability(
                        "infrastructure.resource.create", keyed(record_payload(key, kind)), principal=principal
                    )

                    self.assertNotEqual(result.get("status"), "awaiting_approval", result)
                    self.assertTrue(ManagedResource.objects.filter(key=key, kind=kind).exists(), result)

    def test_adopting_a_public_kind_is_not_held_and_adopting_an_access_kind_still_is(self):
        """A kind that stands behind access waits for an adoption too, as it always has."""

        for kind in public_kinds():
            with self.subTest(kind=kind):
                result = execute_capability(
                    ADOPT, keyed({"kind": kind, "hostname": "www.example.com"}), principal=agent()
                )

                self.assertNotEqual(result.get("status"), "awaiting_approval", result)
        held = execute_capability(ADOPT, keyed({"kind": access_kind(), "hostname": "example"}), principal=agent())

        self.assertEqual(held.get("status"), "awaiting_approval", held)


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class FloorTests(TestCase):
    """The use case refuses what the hold would have held, by the same rule."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()

    def test_an_agents_direct_write_of_a_public_kind_is_refused_on_either_surface(self):
        for kind in public_kinds():
            for interface in ("mcp", "api"):
                with self.subTest(kind=kind, interface=interface):
                    key = fresh_key()

                    with self.assertRaisesMessage(PolicyError, "This change needs approval."):
                        save_managed_resource(record(key, kind), principal=agent(interface))

                    self.assertFalse(ManagedResource.objects.filter(key=key).exists())

    def test_an_operator_writes_a_public_kind_without_an_approval(self):
        for kind in public_kinds():
            for principal in (device(), cli_principal()):
                with self.subTest(kind=kind, interface=principal.interface):
                    key = fresh_key()
                    self.assertFalse(principal.approved_by)

                    save_managed_resource(record(key, kind), principal=principal)

                    self.assertTrue(ManagedResource.objects.filter(key=key, kind=kind).exists())


class WordsTests(TestCase):
    def test_the_contract_says_a_public_kind_waits(self):
        described = {provider["kind"]: provider for provider in describe_providers()["providers"]}

        for kind in public_kinds():
            with self.subTest(kind=kind):
                self.assertTrue(described[kind]["requires_approval"])

    def test_a_change_names_the_public_kinds_and_an_adoption_does_not(self):
        _, groups = matrix()
        rows = {row.name: row for group in groups for row in group.rows}

        for kind in public_kinds():
            label = lower_first(kind_label(kind))
            with self.subTest(kind=kind):
                self.assertIn(label, rows["infrastructure.resource.update"].default)
                self.assertNotIn(label, rows[ADOPT].default)
