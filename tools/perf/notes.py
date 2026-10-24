"""`mise run perf:note <label> <value> [unit]`: one measurement made by hand, kept in var/perf/notes.jsonl.

A line is one JSON object with label, value, unit and time (RFC 3339), the row
the session log reads: it shows each note timed in its window under Measured.
A value that reads as a number is kept as one.
"""

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NOTES = Path(__file__).resolve().parents[2] / "var" / "perf" / "notes.jsonl"


def value_of(said: str) -> int | float | str:
    """A number when ``said`` is a finite one, which JSON can hold; else the text."""

    try:
        number = float(said)
    except ValueError:
        return said
    if not math.isfinite(number):
        return said
    return int(said) if said.lstrip("+-").isdigit() else number


def note(label: str, value: str, unit: str = "", at: datetime | None = None) -> dict[str, Any]:
    at = at or datetime.now(UTC)
    return {"label": label, "value": value_of(value), "unit": unit, "time": at.isoformat(timespec="seconds")}


def append(row: dict[str, Any], notes: Path = NOTES) -> None:
    notes.parent.mkdir(parents=True, exist_ok=True)
    with notes.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(row) + "\n")


def main(argv: list[str]) -> int:
    label, value, unit = argv[0], argv[1], argv[2] if len(argv) > 2 else ""
    row = note(label, value, unit)
    append(row)
    print(f"Noted in {NOTES.relative_to(NOTES.parents[2])}: {json.dumps(row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
