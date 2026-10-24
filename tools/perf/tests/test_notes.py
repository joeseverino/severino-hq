"""perf:note keeps a measurement as the row the session log reads under Measured."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.test import SimpleTestCase

from tools.perf import notes


class NoteTests(SimpleTestCase):
    def test_each_note_is_a_line_of_label_value_unit_and_an_rfc_3339_time(self):
        with tempfile.TemporaryDirectory() as folder:
            kept = Path(folder) / "perf" / "notes.jsonl"
            notes.append(notes.note("cold start", "2.83", "s", datetime(2026, 10, 24, 12, tzinfo=UTC)), kept)
            notes.append(notes.note("worker", "idle"), kept)
            rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(
            rows[0], {"label": "cold start", "value": 2.83, "unit": "s", "time": "2026-10-24T12:00:00+00:00"}
        )
        self.assertEqual(rows[1]["value"], "idle")
        self.assertIsNotNone(datetime.fromisoformat(rows[1]["time"]).tzinfo)

    def test_a_count_stays_an_integer_and_what_json_cannot_hold_stays_text(self):
        self.assertEqual(notes.note("queries", "42")["value"], 42)
        for said in ("nan", "inf", "12 ms"):
            with self.subTest(said=said):
                self.assertEqual(notes.note("x", said)["value"], said)
