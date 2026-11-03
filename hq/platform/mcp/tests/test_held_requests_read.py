"""The held requests an agent's client reads, through the SDK's own client:
the ``hq://approvals`` resource and the ``held-requests`` prompt carry only
what that agent asked for, while a person's device reads every one."""

import json

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from mcp import Client

from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import DEVICE_INTERFACE, Capability, Principal
from hq.platform.application.tests.test_approvals import POLICY_KEY, declare_policy, reconcile_payload

from ..identity import set_principal
from ..server import mcp

GRANT = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})
FIRST = Principal("example-agent", "mcp", GRANT)
SECOND = Principal("example-other-agent", "mcp", GRANT)
DEVICE = Principal("example-phone", DEVICE_INTERFACE, frozenset({Capability.READ}))


def _askers(principal) -> dict[str, set[str]]:
    """Who asked each held request ``principal`` reads, by where it read them."""

    async def steps(client):
        resource = await client.read_resource("hq://approvals")
        prompt = await client.get_prompt("held-requests")
        return json.loads(resource.contents[0].text), prompt.messages[0].content.text.partition("\n")[2]

    async def run():
        async with Client(mcp.server) as client:
            return await steps(client)

    with set_principal(principal):
        resource, prompt = async_to_sync(run)()
    return {
        "resource": {item["requested_actor"] for item in resource["items"]},
        "prompt": {item["requested_actor"] for item in json.loads(prompt)["items"]},
    }


@override_settings(SEVERINO_AGENT_ENABLE_INFRASTRUCTURE=True)
class HeldRequestsReadTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()
        for asker in (FIRST, SECOND):
            answer = execute_capability(
                "infrastructure.reconcile", reconcile_payload(), principal=asker, target=POLICY_KEY
            )
            self.assertTrue(answer["ok"], answer)

    def test_an_agent_reads_only_what_it_asked_for(self):
        self.assertEqual(_askers(SECOND), {"resource": {SECOND.actor}, "prompt": {SECOND.actor}})

    def test_a_persons_device_reads_every_held_request(self):
        both = {FIRST.actor, SECOND.actor}
        self.assertEqual(_askers(DEVICE), {"resource": both, "prompt": both})
