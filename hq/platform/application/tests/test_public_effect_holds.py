"""An agent's change the public internet would see waits for a person, as its delete does.

What counts as public is the provider's ``public_effect``, the host's one
definition of it, so a kind that gains it is held without anything here
naming it.
"""

from itertools import count

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.testing import declare, managing_everything

from ..adoption import ADOPT, ADOPT_SERVICE
from ..capabilities import capability_registry, execute_capability
from ..default_hold import may_be_held_by_default
from ..integration_specs import capability_schema
from ..security import Capability, Principal, could_hold
from ..zones import ADOPT_ZONE
from .writers import keyed

COPIES_LIVE = frozenset({ADOPT, ADOPT_SERVICE, ADOPT_ZONE})
EVERYTHING = frozenset(str(capability) for capability in Capability)
_keys = count()


def public_kinds() -> list[str]:
    """The kinds held only for what the public sees, so nothing else explains a hold."""

    return [kind for kind, provider in PROVIDERS.items() if provider.public_effect and not provider.requires_approval]


def fresh_key() -> str:
    return f"example-{next(_keys)}"


def agent(actor: str) -> Principal:
    """An agent a person signed in, so nothing it asks for is refused for want of one."""

    return Principal(actor, "mcp", EVERYTHING, granted=EVERYTHING, person_key="example")


def declared(kind: str) -> ManagedResource:
    key = fresh_key()
    declare(key, kind, dict(PROVIDERS[kind].sample_record or {}))
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
    for spec in capability_registry():
        if not may_be_held_by_default(spec) or spec.name in COPIES_LIVE:
            continue
        if not could_hold("mcp", spec.required_capabilities):
            continue
        fields = set(capability_schema(spec).get("properties", {}))
        if spec.target_kind is None and "kind" not in fields:
            continue
        reached.append((spec.name, spec, fields))
    return reached


@override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
class HoldTests(TestCase):
    def test_every_change_an_agent_asks_for_on_a_public_kind_waits(self):
        managing_everything()
        self.assertTrue(public_kinds() and reaching_a_record(), "nothing here for the hold to guard")
        for kind in public_kinds():
            for name, spec, fields in reaching_a_record():
                with self.subTest(kind=kind, capability=name):
                    resource = declared(kind)
                    target = resource.key if spec.target_kind is not None else None
                    key = resource.key if target else fresh_key()
                    payload = record_payload(key, kind) if "spec" in fields else least_payload(spec, fields)

                    result = execute_capability(name, keyed(payload), principal=agent(key), target=target)

                    self.assertEqual(result.get("status"), "awaiting_approval", result)
                    self.assertEqual(ManagedResource.objects.get(key=resource.key).generation, resource.generation)
                    self.assertFalse(ManagedResource.objects.filter(key=key).exclude(pk=resource.pk).exists())
                    self.assertFalse(OperationRequest.objects.exists())
