"""A stream held back an event while its agent was refused hears it once a person lifts the brake."""

import asyncio

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from mcp.server.subscriptions import ToolsListChanged

from hq.platform.application import roles
from hq.platform.application.agent_access import SET_AGENT_ACCESS
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.security import Capability, Principal, web_principal
from hq.platform.application.tests.writers import keyed
from hq.platform.core.outside_requests import connections_of_its_own

from ..identity import set_principal
from ..listen import STREAMS

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))


# Only how long a broken path takes to show it: a working one hears at once.
PATIENCE = 20


class ResumeTests(TransactionTestCase):
    """The brake lifted through the capability a person runs. TransactionTestCase
    because the brakes are asked on a pooled thread and lifted on another, as in production."""

    def test_a_stream_held_back_while_agents_are_paused_hears_once_they_resume(self):
        person = get_user_model().objects.create_user("example-operator")
        roles.give_operator(person)

        def pausing(paused):
            with connections_of_its_own():
                answer = execute_capability(
                    SET_AGENT_ACCESS, keyed({"paused": paused}), principal=web_principal(person)
                )
            self.assertTrue(answer["ok"], answer)

        async def run():
            heard, arrived = [], asyncio.Event()

            def listener(event):
                heard.append(event)
                arrived.set()

            with set_principal(AGENT):
                close = STREAMS.subscribe(listener)
            try:
                await asyncio.to_thread(pausing, paused=True)
                await STREAMS.publish(ToolsListChanged())
                held_back = list(heard)
                await asyncio.to_thread(pausing, paused=False)
                await asyncio.wait_for(arrived.wait(), PATIENCE)
            finally:
                close()
            return held_back, heard

        held_back, heard = asyncio.run(run())

        self.assertEqual(held_back, [])
        self.assertEqual(heard, [ToolsListChanged()])
