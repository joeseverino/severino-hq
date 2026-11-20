"""What the worker has done with its current claim, kept on disk across a crash.

A claim whose answer was lost is retried under the same name. A run that
started may have changed machines, so it is reported as unknown and never run
again. Each change is renamed into place, so a crash leaves a whole state.
"""

import json
import os
import secrets
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Entry:
    attempt: str
    operation: str = ""
    report: dict[str, Any] | None = field(default=None)


class Journal:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> Entry | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return Entry(str(raw["attempt"]), str(raw.get("operation", "")), raw.get("report"))

    def _write(self, entry: Entry) -> Entry:
        scratch = self._path.with_name(self._path.name + ".new")
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(entry), handle)
            handle.flush()
            os.fsync(handle.fileno())
        scratch.replace(self._path)
        return entry

    def begin(self) -> Entry:
        """The claim to send: the one a crash left unanswered, else a new one."""

        return self.read() or self._write(Entry(secrets.token_hex(16)))

    def started(self, entry: Entry, operation: str) -> Entry:
        return self._write(replace(entry, operation=operation, report=None))

    def finished(self, entry: Entry, report: dict[str, Any]) -> Entry:
        return self._write(replace(entry, report=report))

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)
