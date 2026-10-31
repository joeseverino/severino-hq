"""What the MCP boundary hands its injected hooks: each refusal to the
recorder, each authenticated identity to the observer, and that a hook that
fails changes no answer."""

from hq.platform.application.security import Capability, Principal

from . import test_mcp


class DoorRefusalTests(test_mcp.MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    def _recorder(self):
        seen = []

        async def record(**fields):
            seen.append(fields)

        return record, seen

    def test_a_bad_credential_is_counted_as_unauthenticated_with_its_source(self):
        record, seen = self._recorder()

        self._request(token="wrong", on_denied=record)

        self.assertEqual(seen, [{"reason": "invalid_credential", "source": "100.64.0.10", "authenticated": False}])

    def test_a_missing_credential_is_counted(self):
        record, seen = self._recorder()

        self._request(token=None, on_denied=record)

        self.assertEqual(seen, [{"reason": "missing_credential", "source": "100.64.0.10", "authenticated": False}])

    def test_a_paused_agent_is_recorded_by_name(self):
        record, seen = self._recorder()
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        async def paused():
            return False

        self._request(token=self.A_JWT, verifier=lambda bearer: agent, gate=paused, on_denied=record)

        self.assertEqual(seen, [{"reason": "agents_paused", "actor": "example-agent", "source": "100.64.0.10"}])

    def test_a_recorder_that_fails_does_not_change_the_answer(self):
        async def broken(**fields):
            raise RuntimeError("audit down")

        with self.assertLogs("severino.mcp", "WARNING") as logged:
            status, body = self._request(token="wrong", on_denied=broken)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        self.assertIn("mcp.denial.unrecorded", [record.event for record in logged.records])

    def test_an_admitted_request_records_nothing(self):
        record, seen = self._recorder()

        status, _ = self._request(on_denied=record)

        self.assertEqual(status, 204)
        self.assertEqual(seen, [])


class ObservationTests(test_mcp.MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    def test_an_authenticated_agent_is_observed_even_while_paused(self):
        seen = []
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        async def observer(principal):
            seen.append(principal.actor)

        async def paused():
            return False

        status, _ = self._request(token=self.A_JWT, verifier=lambda bearer: agent, gate=paused, observer=observer)

        self.assertEqual(status, 403)
        self.assertEqual(seen, ["example-agent"])

    def test_a_rejected_credential_is_not_observed(self):
        seen = []

        async def observer(principal):
            seen.append(principal)

        self._request(token="wrong", observer=observer)

        self.assertEqual(seen, [])

    def test_an_observer_that_fails_does_not_change_the_answer(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        async def broken(principal):
            raise RuntimeError("registry down")

        with self.assertLogs("severino.mcp", "WARNING") as logged:
            status, _ = self._request(token=self.A_JWT, verifier=lambda bearer: agent, observer=broken)

        self.assertEqual(status, 204)
        self.assertIn("mcp.identity.unobserved", [record.event for record in logged.records])
