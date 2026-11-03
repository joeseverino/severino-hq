"""The Agents page's read of what each agent did is served by the index on
who acted, however long the log grows."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application import agent_activity
from hq.platform.core.models import AuditLog


def _actor_index() -> str:
    return next((index.name for index in AuditLog._meta.indexes if index.fields[:1] == ["actor"]), "an index on actor")


class RecentActivityPlanTests(TestCase):
    def test_the_read_by_actor_uses_the_actor_index(self):
        AuditLog.objects.create(action=AuditLog.Action.UPDATED, metadata={"actor": "example-agent"})

        with CaptureQueriesContext(connection) as queries:
            agent_activity.recent_activity(["example-agent", "example-other"])
        (read,) = [query["sql"] for query in queries.captured_queries if AuditLog._meta.db_table in query["sql"]]
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN QUERY PLAN {read}")
            plan = "\n".join(str(step[-1]) for step in cursor.fetchall())

        self.assertIn(_actor_index(), plan, plan)
