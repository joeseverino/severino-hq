"""HQ's API served in process through Django's real WSGI stack, for Schemathesis to call.

Schemathesis is the fuzz group's alone, so each module the group's task runs
loads the document itself, and this one, which it imports, never names it.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from django.core import signals
from django.db import close_old_connections
from django.test import TestCase, override_settings

DOCUMENT = "/api/v2/openapi.json"
# Bodies are read as the document describes them. This in-process client does
# not decode gzip; compression is the transport's, tested end to end on its own.
UNCOMPRESSED = {"Accept-Encoding": "identity"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


@override_settings(
    ALLOWED_HOSTS=["localhost", "testserver"],
    SECURE_SSL_REDIRECT=False,
    OIDC_ISSUER="https://sso.example.com",
    SEVERINO_API_RESOURCE="https://hq.example.com/api",
)
class InProcessAPITestCase(TestCase):
    """A test that calls the API in process, where nothing reaches the network."""

    def setUp(self):
        # Any unexpected outbound connection fails rather than reaching a provider.
        self.enterContext(
            patch("socket.socket.connect", side_effect=AssertionError("Outbound network access in an API test"))
        )
        self.enterContext(
            patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset({"192.0.2.1"}))
        )
        # Match Django's ClientHandler: WSGI request signals must keep the test
        # runner's transaction rather than close its SQLite connection.
        for signal in (signals.request_started, signals.request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)


@contextmanager
def as_agent(claims: dict[str, str]) -> Iterator[None]:
    """Every call made inside is an agent's whose token verifies to ``claims``, with agents unpaused."""
    with ExitStack() as stack:
        stack.enter_context(patch("hq.platform.application.agent_admission.verify", return_value=claims))
        stack.enter_context(patch("hq.platform.application.agent_access.paused_interfaces", return_value=frozenset()))
        yield
