"""dashboard_snapshot answers the queue's first page, each item with every
command and address its page item carries, and without what only the page
reads; each list it leaves to another tool, the whole queue's too, it names
the call that reads it."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.dashboard import operating_snapshot
from hq.platform.application.integration_specs import typed_keys
from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.security import Capability, Principal, operator_capabilities
from hq.platform.application.ui import Insight
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..dashboard_answer import ACTION_ITEMS_PAGE, AgentSnapshot
from ..identity import set_principal
from ..server import mcp

READER = Principal("example-reader", "mcp", operator_capabilities())
# Where a page item says what to run or where to go.
ACTED_ON = frozenset({"url", "capability", "target", "summary", "detail", "action"})
# What the page alone reads: the revision a person sets an item aside at, the
# gate the queue was filtered by, the heading items group under, and a plan's
# own id and completion.
PAGE_ONLY = frozenset({"revision", "read_capabilities", "family", "workflow"})
# More than a page of the queue, raised by an extension.
RAISED = tuple(
    Insight("serious", "Alpha", f"Example {n} needs you", "1", "Body.") for n in range(ACTION_ITEMS_PAGE + 2)
)


def _said(value, found=None):
    """Each value ``value`` holds under a key an agent acts on."""

    found = set() if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ACTED_ON and isinstance(inner, str) and inner:
                found.add(inner)
            _said(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _said(inner, found)
    return found


def _record(row):
    """Which record a list's row is: a project or content item by its slug, a
    queue item by what it says needs doing, an event by what and when."""

    return row.get("slug") or row.get("label") or (row["title"], row["created_at"])


class DashboardAnswerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard"):
            pass

    def _answered(self, tool="dashboard_snapshot", arguments=None, principal=READER):
        async def run():
            async with Client(mcp.server) as client:
                return await client.call_tool(tool, arguments or {})

        with set_principal(principal):
            return async_to_sync(run)()

    def _read_whole(self, call):
        """Every row the list ``call`` names, each page followed to the last."""

        rows, arguments = [], dict(call.get("arguments") or {})
        while True:
            read = self._answered(call["tool"], arguments)
            self.assertFalse(read.is_error, read.structured_content)
            rows += read.structured_content.get("items", [])
            if not (after := read.structured_content.get("next")):
                return rows
            arguments["cursor"] = after["cursor"]

    def test_each_item_keeps_what_an_agent_acts_on_and_leaves_the_pages_own_parts(self):
        page = {item["key"]: item for item in operating_snapshot(principal=READER)["priority"]}
        answer = self._answered()

        self.assertFalse(answer.is_error)
        agent = {item["key"]: item for item in answer.structured_content["priority"]}
        self.assertLessEqual(set(agent), set(page))
        self.assertTrue(any(item["workflow"] for item in page.values()), "a plan is read")
        for key, item in agent.items():
            with self.subTest(item=key):
                self.assertLessEqual(_said(page[key]), _said(item))
                self.assertEqual(set(item) & PAGE_ONLY, set())

    def test_the_page_reads_its_own_parts_still(self):
        (item, *_) = operating_snapshot(principal=READER)["priority"]

        self.assertLessEqual(PAGE_ONLY, set(item))

    def test_each_list_it_leaves_out_is_named_by_a_call_that_reads_its_records(self):
        page = operating_snapshot(principal=READER)
        answer = self._answered().structured_content
        lists = answer["lists"]

        # The queue is the one list the answer holds a page of.
        self.assertEqual(set(page) - set(typed_keys(AgentSnapshot)), set(lists) - {"priority"})
        self.assertTrue(any(page[part] for part in lists), "a list holds records")
        for part, call in lists.items():
            with self.subTest(part=part):
                items = self._read_whole(call)
                self.assertLessEqual({_record(row) for row in page[part]}, {_record(item) for item in items})

    def test_a_queue_longer_than_a_page_is_answered_its_first_page_and_counted_whole(self):
        with installed(sibling(attention=RAISED)):
            queue = [item["label"] for item in operating_snapshot(principal=READER)["priority"]]
            answer = self._answered().structured_content
            call = answer["lists"]["priority"]
            first = self._answered(call["tool"], call["arguments"]).structured_content
            walked = [item["label"] for item in self._read_whole(call)]

        self.assertGreater(len(queue), ACTION_ITEMS_PAGE)
        self.assertEqual([item["label"] for item in answer["priority"]], queue[:ACTION_ITEMS_PAGE])
        self.assertEqual(answer["priority_count"], len(queue))
        self.assertEqual((first["count"], first["truncated"]), (ACTION_ITEMS_PAGE, True))
        self.assertEqual(walked, queue)

    def test_a_caller_who_may_not_read_the_audit_log_is_not_pointed_at_it(self):
        reader = Principal("example-reader", "mcp", frozenset({Capability.READ}))

        answer = self._answered(principal=reader).structured_content

        self.assertNotIn("recent_activity", answer["lists"])
        self.assertTrue(any("recent_activity" in said for said in answer["unread"]))
