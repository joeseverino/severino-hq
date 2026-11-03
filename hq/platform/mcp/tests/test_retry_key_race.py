"""Two MCP writes in flight together with one retry key act once.

Each runs on a thread with a database connection of its own, as each MCP
request now does, against the WAL SQLite file production writes.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import TransactionTestCase

from hq.domains.projects.models import Project
from hq.platform.application.security import Principal

from .. import services
from ..identity import set_principal

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects"}))
PAYLOAD = {"name": "Once", "slug": "once", "status": "active", "idempotency_key": "example-race-1"}


class RetryKeyRaceTests(TransactionTestCase):
    def test_two_writes_at_once_with_one_key_act_once(self):
        together = threading.Barrier(2, timeout=30)

        def write(_):
            try:
                with set_principal(WRITER):
                    together.wait()
                    return services.execute_capability("project.create", dict(PAYLOAD))
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(write, range(2))

        self.assertTrue(first["ok"], first)
        self.assertEqual(second, first)
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)
