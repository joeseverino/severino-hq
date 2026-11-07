"""Every declared tool's answer stays within the bytes its declaration budgets, on the standard data set.

The calls are the declarations' own (``shapes.Budget``), so a tool declared
later is held here as it is declared. Each is answered in a savepoint rolled
back after it, so no call's writes (a project, an export's audit row) weigh
on another's answer.
"""

import json

from asgiref.sync import async_to_sync
from django.db import transaction
from django.test import TestCase
from mcp import Client

from hq.platform.application.security import Principal, operator_capabilities
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..identity import set_principal
from ..server import mcp
from ..shapes import declared

# An agent holding every grant, an extension's too, so each tool answers rather than refuses.
READER = Principal("example-reader", "mcp", operator_capabilities())


def plain(call):
    """A declared call as JSON sends it."""

    return json.loads(json.dumps(call, default=dict))


class AnswerBudgetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard"):
            pass

    def _answered(self, name, arguments):
        async def run():
            async with Client(mcp.server) as client:
                return await client.call_tool(name, arguments)

        with set_principal(READER), transaction.atomic():
            answer = async_to_sync(run)()
            transaction.set_rollback(True)
        return answer

    def test_every_tools_answer_is_within_its_budget(self):
        for name, tool in mcp.tools.items():
            budget = declared(tool.handler).budget
            answer = self._answered(name, plain(budget.call))
            with self.subTest(tool=name):
                self.assertFalse(answer.is_error, answer.structured_content)
                size = len(answer.content[0].text.encode())
                self.assertLessEqual(size, budget.bytes, f"{name} answered {size} bytes")
