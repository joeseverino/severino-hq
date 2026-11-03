"""Two writes in flight together with one retry key act once, though one came by the API and one by MCP.

Each runs on a thread with a database connection of its own, against the WAL
SQLite file production writes (``hq/platform/core/test_runner.py``).
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings

from hq.domains.projects.models import Project
from hq.platform.application import tokens
from hq.platform.mcp import services
from hq.platform.mcp.identity import set_principal

from .tokens import ISSUER, RESOURCE, _serving, _token

CLIENT = "example-agent"
KEY = "example-race-1"
PROJECT = {"name": "Once", "slug": "once", "status": "active"}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class RetryKeyRaceTests(TransactionTestCase):
    def test_two_writes_at_once_by_two_doors_with_one_key_act_once(self):
        together = threading.Barrier(2, timeout=30)
        token = _token(CLIENT, scope="write_projects")
        claims = {"client_id": CLIENT, "sub": CLIENT, "scope": "write_projects"}

        def by_api():
            together.wait()
            response = Client().post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": PROJECT}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY=KEY,
            )
            answer = response.json()
            return answer["ok"], answer.get("data", answer.get("error"))

        def by_mcp():
            with set_principal(tokens.token_principal(claims, "mcp")):
                together.wait()
                answer = services.execute_capability("project.create", PROJECT | {"idempotency_key": KEY})
            return answer["ok"], {name: value for name, value in answer.items() if name != "ok"}

        def run(door):
            try:
                return door()
            finally:
                connection.close()

        with _serving(), ThreadPoolExecutor(max_workers=2) as pool:
            api, mcp = pool.map(run, (by_api, by_mcp))

        self.assertTrue(api[0], api)
        self.assertEqual(mcp, api)
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)
