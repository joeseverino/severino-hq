"""HQ's bus over the SDK's: an event reaches only the streams it is aimed at,
and a caller the brakes refuse is held back what it is owed until it may act.

The listener is called as an event is published, so what each stream heard
is read at once. The door's own tests are in ``test_listen``.
"""

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase, override_settings
from mcp.server.subscriptions import ToolsListChanged

from hq.platform.application.security import DEVICE_INTERFACE, Capability, Principal

from ..identity import set_principal
from ..listen import Streams
from ..security import token_expiry

PAUSED = "hq.platform.application.agent_access.paused_interfaces"
BLOCKED = "hq.platform.application.agent_access.blocked"
STANDING = "hq.platform.application.agent_devices.standing"
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))
OTHER = Principal("example-other", "mcp", frozenset({Capability.READ}))


class HeldBackTests(TestCase):
    """The bus itself, whose listener is called as an event is published."""

    def _published(self, principal, steps):
        streams, heard = Streams(), []

        async def run():
            with set_principal(principal):
                close = streams.subscribe(heard.append)
            try:
                await steps(streams, heard)
            finally:
                close()

        async_to_sync(run)()
        return heard

    def test_a_paused_agent_keeps_its_stream_and_hears_once_resumed(self):
        heard_while_paused = []

        async def steps(streams, heard):
            with mock.patch(PAUSED, return_value=frozenset({"mcp", "api"})):
                await streams.publish(ToolsListChanged())
                heard_while_paused.extend(heard)
            await streams.settle_owed()

        heard = self._published(AGENT, steps)

        self.assertEqual(heard_while_paused, [])
        self.assertEqual(heard, [ToolsListChanged()])

    def test_a_blocked_agent_hears_nothing_while_another_agent_hears(self):
        others = []

        async def steps(streams, heard):
            with set_principal(OTHER):
                close = streams.subscribe(others.append)
            with mock.patch(BLOCKED, side_effect=lambda client_id: client_id == AGENT.actor):
                await streams.publish(ToolsListChanged())
                await streams.settle_owed()
            close()

        heard = self._published(AGENT, steps)

        self.assertEqual(heard, [])
        self.assertEqual(others, [ToolsListChanged()])

    def test_an_agent_on_an_unapproved_device_hears_once_it_is_approved(self):
        from hq.platform.application.agent_devices import DeviceStanding

        heard_awaiting = []

        async def steps(streams, heard):
            with mock.patch(STANDING, return_value=DeviceStanding.AWAITING):
                await streams.publish(ToolsListChanged())
                heard_awaiting.extend(heard)
            with mock.patch(STANDING, return_value=DeviceStanding.APPROVED):
                await streams.settle_owed()

        heard = self._published(AGENT, steps)

        self.assertEqual(heard_awaiting, [])
        self.assertEqual(heard, [ToolsListChanged()])

    def test_an_event_reaches_only_the_streams_it_is_aimed_at(self):
        device = Principal("example-phone", DEVICE_INTERFACE, frozenset({Capability.READ}), person_key="example-key")
        others = []

        async def steps(streams, heard):
            with set_principal(device):
                close = streams.subscribe(others.append)
            await streams.publish(ToolsListChanged(), aimed=lambda principal: principal.person_key == "example-key")
            close()

        heard = self._published(AGENT, steps)

        self.assertEqual(heard, [])
        self.assertEqual(others, [ToolsListChanged()])


class TokenExpiryTests(SimpleTestCase):
    @override_settings(SEVERINO_API_LEEWAY_SECONDS=30)
    def test_a_stream_lasts_as_long_as_the_admission_would_take_its_token(self):
        from hq.platform.api.tests.tokens import _token

        expires = 2_000_000_000

        self.assertEqual(token_expiry(_token(exp=expires)), expires + 30)
