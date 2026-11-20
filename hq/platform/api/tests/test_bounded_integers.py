"""Every integer a caller sends names its ceiling: a type that holds one
naming none does not build its schema, and one past it is refused as invalid,
never carried to the database, whose integer it would overflow. A record's id
is ``record_fields.RecordId``, bounded by the most one may be, on every door."""

from dataclasses import dataclass

from django.test import SimpleTestCase, TestCase

from hq.domains.docs_index.models import DocumentationRecord

from .doors import MCP, OPERATOR, SHELL, Caller, at_the_door, each_door

# One past the most a record's id may be.
PAST = 2**63
WRITER = Caller("example-writer", "read write_documentation write_expenses read_audit_log")
DOC = {"doc_id": "example-doc", "title": "Example"}


@dataclass(frozen=True)
class ExampleCount:
    counts: list[int]


class UnboundedTests(SimpleTestCase):
    def test_a_sent_type_holding_an_integer_with_no_ceiling_does_not_build(self):
        from hq.platform.application.json_schemas import sent_schema

        with self.assertRaisesRegex(TypeError, "#/properties/counts/items"):
            sent_schema(ExampleCount)


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
