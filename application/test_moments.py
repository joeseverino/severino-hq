"""One parser for an instant, one renderer for it.

About twenty modules parsed timestamps by hand and disagreed at the edges:
some kept a naive result, one replaced an offset with UTC instead of
converting it, one raised on a value it could not read. ``ui.moment`` reads
and ``projection.iso`` writes, and a test keeps new copies out.
"""

import ast
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase

from .projection import iso
from .ui import moment

# Parsers that mean something different on purpose, each named with why.
DELIBERATE = {
    # An expiry without an offset is refused, not assumed to be UTC.
    "credential_mint.py",
    # Dates, not instants.
    "analytics.py",
    # The one parser.
    "ui.py",
}


class MomentTests(SimpleTestCase):
    def test_an_offset_is_kept_rather_than_replaced(self):
        found = moment("2026-03-01T12:00:00+02:00")

        self.assertEqual(found, datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(found.utcoffset(), timedelta(hours=2))

    def test_a_stamp_without_an_offset_is_utc(self):
        self.assertEqual(moment("2026-03-01T12:00:00"), datetime(2026, 3, 1, 12, tzinfo=UTC))
        self.assertEqual(moment("2026-03-01T12:00:00Z"), datetime(2026, 3, 1, 12, tzinfo=UTC))

    def test_a_datetime_passes_through_and_becomes_aware(self):
        aware = datetime(2026, 3, 1, 12, tzinfo=timezone(timedelta(hours=-5)))

        self.assertIs(moment(aware), aware)
        self.assertEqual(moment(datetime(2026, 3, 1, 12)).tzinfo, UTC)

    def test_nothing_readable_is_none_not_an_exception(self):
        for value in (None, "", "   ", "not a date", "0001-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assertIsNone(moment(value))


class IsoTests(SimpleTestCase):
    def test_an_instant_renders_and_absence_is_null(self):
        self.assertEqual(iso(datetime(2026, 3, 1, 12, tzinfo=UTC)), "2026-03-01T12:00:00+00:00")
        self.assertIsNone(iso(None))
        self.assertIsNone(iso(""))

    def test_a_value_already_rendered_passes_through(self):
        self.assertEqual(iso("2026-03-01"), "2026-03-01")


class OneParserTests(SimpleTestCase):
    def test_application_code_parses_instants_through_moment(self):
        root = Path(__file__).resolve().parent
        found = []
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("test") or path.name in DELIBERATE:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "fromisoformat"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "datetime"
                ):
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [])
