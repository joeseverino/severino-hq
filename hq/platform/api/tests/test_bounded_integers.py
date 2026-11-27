"""Every integer a caller sends names its ceiling: a type that holds one
naming none does not build its schema, so the document bounds each one and the
property suite refuses one past it before it reaches the database, whose
integer it would overflow."""

from dataclasses import dataclass

from django.test import SimpleTestCase


@dataclass(frozen=True)
class ExampleCount:
    counts: list[int]


class UnboundedTests(SimpleTestCase):
    def test_a_sent_type_holding_an_integer_with_no_ceiling_does_not_build(self):
        from hq.platform.application.json_schemas import sent_schema

        with self.assertRaisesRegex(TypeError, "#/properties/counts/items"):
            sent_schema(ExampleCount)
