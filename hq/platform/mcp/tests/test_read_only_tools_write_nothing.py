"""A tool annotated ``readOnlyHint`` writes nothing: a client calls one without asking, so the hint is held by calling it.

Each tool the listing calls read-only is called as an agent holding every
capability, every resource kind for the resource tools, and the database is
watched for a write. The cache table is not HQ's record of anything, so a
read that fills it still reads only.
"""

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from mcp import types

from hq.domains.projects.models import Project
from hq.platform.application.security import Capability, Principal

from ..contract import served_resources
from ..identity import set_principal
from ..server import mcp

WRITES = ("insert", "update", "delete", "replace")
# An agent holding every capability, so each kind is read rather than refused.
READER = Principal("example-reader", "mcp", frozenset(Capability))
# What a tool with a required argument is called with; one missing here is refused, and so never answers.
ARGUMENTS = {"export_year_summary": {"year": 2026}}


def _calls(slug):
    """Every call a read-only tool is tried with: its defaults, and each kind a resource tool names."""

    contract = served_resources()
    for name, tool in mcp.tools.items():
        if not tool.annotations["readOnlyHint"]:
            continue
        if name == "list_resource":
            yield from ((name, {"name": kind.name}) for kind in contract.listable)
        elif name == "get_resource":
            yield from ((name, {"name": kind.name, "identifier": "example-absent"}) for kind in contract.addressable)
            yield name, {"name": "projects", "identifier": slug}
        else:
            yield name, ARGUMENTS.get(name, {})


def _cache_tables():
    return {entry["LOCATION"].lower() for entry in settings.CACHES.values() if "LOCATION" in entry}


class ReadOnlyToolsWriteNothingTests(TestCase):
    def setUp(self):
        self.enterContext(set_principal(READER))
        self.project = Project.objects.create(name="Read only", slug="read-only")

    def test_every_tool_annotated_read_only_writes_no_row(self):
        cache = _cache_tables()
        answered = set()
        for name, arguments in _calls(self.project.slug):
            with self.subTest(tool=name, arguments=arguments):
                params = types.CallToolRequestParams(name=name, arguments=arguments)
                with CaptureQueriesContext(connection) as queries:
                    answer = async_to_sync(mcp._call)(None, params)
                if answer.is_error and answer.structured_content["error"]["code"] != "not_found":
                    # A refused call ran nothing of the tool; its denial row is the refusal's record.
                    continue
                answered.add(name)
                writes = [
                    query["sql"]
                    for query in queries.captured_queries
                    if query["sql"].lstrip().lower().startswith(WRITES)
                    and not any(table in query["sql"].lower() for table in cache)
                ]
                self.assertEqual(writes, [], f"{name} is annotated readOnlyHint and wrote")
        # The guard reads every read-only tool, so it cannot pass by being refused each one.
        read_only = {name for name, tool in mcp.tools.items() if tool.annotations["readOnlyHint"]}
        self.assertEqual(answered, read_only)
