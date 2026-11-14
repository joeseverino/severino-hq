"""``hq_call --agent``, a development server's agent door: there only with DEBUG
on a server loopback alone reaches, holding the grant named and no more, and
meeting the admission, brakes, authorization and approval an agent meets over
/mcp/."""

import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.application.security import AGENT_SURFACES
from hq.platform.core.attribution import command_operation, from_the_shell
from hq.platform.core.models import AgentIdentity, AuditLog

from .procedures import RESTART_RUN, Supply, manifest, run_payload

AGENT = "example-agent"
LOGS = "example.service_logs"
LOOPBACK_ONLY = override_settings(DEBUG=True, ALLOWED_HOSTS=["127.0.0.1", "localhost", "[::1]"])
READ = {"tool": "audit_registry"}


def _ask(name, **fields):
    return {"tool": "execute_capability", "arguments": {"name": name, "payload": run_payload(**fields)}}


CHANGE = _ask(RESTART_RUN, idempotency_key="example-ask")
READ_RUN = _ask(f"automation.{LOGS}.run")


def _as_agent(request, *grants):
    """What the command wrote for ``request`` made as the agent holding
    ``grants``, and the error it exited with, if any, in a shell bound as
    ``manage.py`` binds every command."""

    arguments = ["--agent", AGENT, *(word for grant in grants for word in ("--grant", grant))]
    output = StringIO()
    failed = None
    with (
        mock.patch("sys.stdin", StringIO(json.dumps(request))),
        from_the_shell(command_operation(["manage.py", "hq_call"])),
    ):
        try:
            call_command("hq_call", *arguments, stdout=output)
        except CommandError as error:
            failed = error
    return json.loads(output.getvalue()) if output.getvalue() else None, failed


class AgentDoorTests(TestCase):
    def setUp(self):
        Supply(self, manifest(), manifest(id=LOGS, effect="read", retry="safe"))

    def test_without_debug_there_is_no_agent_door(self):
        with override_settings(DEBUG=False, ALLOWED_HOSTS=["127.0.0.1"]):
            answer, failed = _as_agent(READ, "read")

        self.assertIsNone(answer)
        self.assertIsNotNone(failed)
        self.assertFalse(AgentIdentity.objects.exists())

    def test_a_server_reached_past_loopback_has_no_agent_door(self):
        for hosts in (["127.0.0.1", "hq.example.com"], ["192.0.2.7"], ["*"], [".localhost.example.com"], []):
            with self.subTest(hosts=hosts), override_settings(DEBUG=True, ALLOWED_HOSTS=hosts):
                answer, failed = _as_agent(READ, "read")

                self.assertIsNone(answer)
                self.assertIsNotNone(failed)
        self.assertFalse(AgentIdentity.objects.exists())

    @LOOPBACK_ONLY
    def test_the_agent_holds_the_grant_named_and_nothing_more(self):
        from hq.platform.core.management.commands.hq_call import agent_door

        self.assertEqual(agent_door(AGENT, ["read"]).capabilities, {"read"})
        self.assertEqual(
            agent_door(AGENT, ["read,manage_infrastructure"]).capabilities, {"read", "manage_infrastructure"}
        )

    @LOOPBACK_ONLY
    def test_a_grant_no_agent_can_carry_is_refused(self):
        # An unknown name, one held only at a person's own interfaces, and none.
        for grants in (("read", "example_unknown"), ("adopt_playbooks",), (" , ",), ()):
            with self.subTest(grants=grants):
                answer, failed = _as_agent(READ, *grants)

                self.assertIsNone(answer)
                self.assertIsNotNone(failed)
        self.assertFalse(AgentIdentity.objects.exists())

    @LOOPBACK_ONLY
    def test_a_grant_without_an_agent_is_refused(self):
        output = StringIO()
        with self.assertRaises(CommandError), mock.patch("sys.stdin", StringIO(json.dumps(READ))):
            call_command("hq_call", "--grant", "read", stdout=output)
        self.assertEqual(output.getvalue(), "")

    @LOOPBACK_ONLY
    def test_a_read_run_is_queued_unheld_as_the_agents_own(self):
        answer, failed = _as_agent(READ_RUN, "read")

        self.assertIsNone(failed)
        self.assertTrue(answer["ok"])
        self.assertFalse(ApprovalRequest.objects.exists())
        queued = OperationRequest.objects.get()
        self.assertEqual((queued.requested_actor, queued.requested_interface), (AGENT, "mcp"))
        self.assertEqual(AgentIdentity.objects.get().client_id, AGENT)

    @LOOPBACK_ONLY
    def test_a_change_the_grant_lacks_is_refused(self):
        answer, failed = _as_agent(CHANGE, "read")

        self.assertEqual(answer["ok"], False)
        self.assertIsNotNone(failed)
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertFalse(OperationRequest.objects.exists())

    @LOOPBACK_ONLY
    def test_a_change_it_may_ask_for_is_held_for_a_person(self):
        answer, failed = _as_agent(CHANGE, "read", "manage_infrastructure")

        self.assertIsNone(failed)
        self.assertEqual(answer["status"], "awaiting_approval")
        held = ApprovalRequest.objects.get()
        self.assertEqual((held.requested_actor, held.requested_interface), (AGENT, "mcp"))
        self.assertFalse(OperationRequest.objects.exists())
        self.assertTrue(AuditLog.objects.filter(actor=AGENT, interface="mcp").exists())
        self.assertFalse(AuditLog.objects.exclude(actor=AGENT).exists())

    @LOOPBACK_ONLY
    def test_it_is_braked_as_an_agent_over_mcp_is(self):
        with mock.patch("hq.platform.application.agent_access.paused_interfaces", return_value=AGENT_SURFACES):
            answer, failed = _as_agent(READ_RUN, "read")

        self.assertEqual(answer and answer["error"]["code"], "agents_paused")
        self.assertIsNotNone(failed)
        self.assertFalse(OperationRequest.objects.exists())
