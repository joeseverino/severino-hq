"""Every integer a caller sends the API declares its ceiling: one past it is
refused as invalid where the document says so, never carried to the
database, whose integer it would overflow. A record's id is
``record_fields.RecordId``, bounded by the most one may be, on every door."""

from collections.abc import Iterator, Mapping
from typing import Any

from django.test import SimpleTestCase, TestCase

from hq.domains.docs_index.models import DocumentationRecord

from ..openapi import document
from .doors import MCP, OPERATOR, SHELL, Caller, at_the_door, each_door

BOUNDS = frozenset({"maximum", "exclusiveMaximum", "enum", "const"})
# One past the most a record's id may be.
PAST = 2**63
WRITER = Caller("example-writer", "read write_documentation write_expenses read_audit_log")
DOC = {"doc_id": "example-doc", "title": "Example"}


def _resolved(root: Mapping[str, Any], reference: str) -> Any:
    found: Any = root
    for part in reference.removeprefix("#/").split("/"):
        found = found[part]
    return found


def _unbounded(root: Mapping[str, Any], node: Any, where: str, seen: frozenset[str] = frozenset()) -> Iterator[str]:
    """Where under ``node`` an integer declares no ceiling, its references followed once each."""

    if isinstance(node, list):
        for index, item in enumerate(node):
            yield from _unbounded(root, item, f"{where}[{index}]", seen)
        return
    if not isinstance(node, dict):
        return
    if reference := node.get("$ref"):
        # A use may bound the type it refers to beside the reference.
        if reference not in seen and not BOUNDS & node.keys():
            yield from _unbounded(root, _resolved(root, reference), f"{where}->{reference}", seen | {reference})
        return
    kinds = node.get("type")
    if (kinds == "integer" or (isinstance(kinds, list) and "integer" in kinds)) and not BOUNDS & node.keys():
        yield where
    for key, value in node.items():
        if key not in {"examples", "example", "default"}:
            yield from _unbounded(root, value, f"{where}.{key}", seen)


class BoundedIntegerTests(SimpleTestCase):
    def test_every_integer_a_request_carries_declares_its_ceiling(self):
        root = document()
        found = []
        for path, methods in root["paths"].items():
            for method, operation in methods.items():
                if not isinstance(operation, dict):
                    continue
                sent = {"parameters": operation.get("parameters", ()), "body": operation.get("requestBody", {})}
                found.extend(_unbounded(root, sent, f"{method.upper()} {path}"))

        self.assertEqual(found, [])


class PastTheCeilingTests(TestCase):
    """An id past the most one may be is refused as invalid on every door, wherever it is sent."""

    def setUp(self):
        at_the_door(self)

    def test_an_id_past_the_ceiling_is_invalid_in_a_payload_a_target_and_a_read(self):
        for caller in (WRITER, OPERATOR):
            for door in each_door(self, caller, "execute_capability", "get_resource"):
                with door:
                    for answer in (
                        door.capability(
                            "documentation.create", DOC | {"related_expenses": [PAST], "idempotency_key": "a"}
                        ),
                        door.capability("expense.update", {"notes": "Kept.", "idempotency_key": "b"}, target=PAST),
                        door("get_resource", name="playbook_scans", identifier=PAST),
                    ):
                        self.assertEqual(answer["error"]["code"], "invalid_input", answer)
                    self.assertFalse(DocumentationRecord.objects.exists())

    def test_a_count_past_the_ceiling_is_invalid_where_a_tool_takes_one(self):
        for caller in (WRITER, OPERATOR):
            for door in each_door(self, caller, "recent_activity", doors=(MCP, SHELL)):
                with door:
                    answer = door("recent_activity", limit=PAST)

                    self.assertEqual(answer["error"]["code"], "invalid_input", answer)
