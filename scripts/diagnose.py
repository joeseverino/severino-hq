#!/usr/bin/env python3
"""Say why a pipeline run failed, from its log, in plain words.

    gh run view RUN --log-failed | scripts/diagnose.py

Prints ``{"id", "title", "fix"}`` for the first diagnosis in
deploy/diagnoses.json whose pattern the log matches, or a general one when
none does. Only the catalog's own text is ever printed, never a line of the
log, so what it says is safe to post on a public repository.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).resolve().parents[1] / "deploy" / "diagnoses.json"
UNKNOWN = {
    "id": "unknown",
    "title": "A step failed",
    "fix": "No known cause matched. The failing step's log says what happened; once it is understood, add it to deploy/diagnoses.json so it is recognised next time.",
}


def diagnoses() -> list[dict[str, Any]]:
    return json.loads(CATALOG.read_text())["diagnoses"]


# GitHub echoes each step's script and environment between these markers, so
# every message a step can print is in its log whether or not it ran.
ECHOED = re.compile(r"^[^\n]*##\[group\]Run .*?^[^\n]*##\[endgroup\][^\n]*$", re.MULTILINE | re.DOTALL)


def diagnose(log: str) -> dict[str, str]:
    printed = ECHOED.sub("", log)
    for entry in diagnoses():
        if re.search(entry["match"], printed):
            return {key: entry[key] for key in ("id", "title", "fix")}
    return dict(UNKNOWN)


if __name__ == "__main__":
    print(json.dumps(diagnose(sys.stdin.read())))
