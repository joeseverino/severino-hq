"""What the MCP boundary hands HQ's admission: each bearer with the peer that
sent it, and that an admission that fails answers as no admission at all."""

from asgiref.sync import async_to_sync

from hq.platform.application.security import Capability, Principal

from ..security import MCPBoundary, TailnetGate
from . import test_mcp


class DoorAdmissionTests(test_mcp.MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    def test_a_bad_credential_reaches_the_admission_with_its_source(self):
        seen = []

        self._request(token="wrong", seen=seen)

        self.assertEqual(seen, [("wrong", "100.64.0.10")])

    def test_a_missing_credential_reaches_the_admission_too(self):
        """So a request with no token is recorded as one, as on the machine API."""

        for headers in ((), ((b"authorization", b"Basic dXNlcjpwdw=="),)):
            with self.subTest(headers=headers):
                seen = []

                self._request(token=None, seen=seen, extra_headers=headers)

                self.assertEqual(seen, [("", "100.64.0.10")])

    def test_an_admission_that_fails_refuses(self):
        def broken(bearer):
            raise RuntimeError("database locked")

        with self.assertLogs("severino.mcp", "WARNING") as logged:
            status, body = self._request(verifier=broken)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "invalid_token")
        self.assertIn("mcp.token.unadmitted", [record.event for record in logged.records])

    def test_a_boundary_with_no_admission_admits_no_bearer(self):
        boundary = MCPBoundary(
            self._allowed_app,
            gate=TailnetGate(allowed_hosts=("a-docker-host",), allowed_networks=("100.64.0.0/10",)),
            admit=None,
        )

        self.assertIsNone(async_to_sync(boundary._admitted)(self.A_JWT, "100.64.0.10"))

    def test_a_refused_network_reaches_no_admission(self):
        for refused in ({"client": "192.0.2.10"}, {"host": "hq.example.com"}, {"origin": "https://evil.example"}):
            with self.subTest(refused=refused):
                seen = []

                self._request(seen=seen, **refused)

                self.assertEqual(seen, [])

    def test_an_admitted_agent_is_bound_once(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        seen = []

        status, _ = self._request(token=self.A_JWT, verifier=lambda bearer: agent, seen=seen)

        self.assertEqual(status, 204)
        self.assertEqual(seen, [(self.A_JWT, "100.64.0.10")])
