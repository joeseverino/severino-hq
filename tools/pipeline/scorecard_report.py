"""Judge a local OpenSSF Scorecard run, and exit non-zero on any finding.

Reads the JSON that `scorecard --local <tree> --show-details --format json`
writes. Every check in CHECKS must score 10 and the STRICT ones must carry no
warning. The closing line names each check that failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The checks local mode can answer from files alone. The rest (Branch-Protection,
# Code-Review, CII-Best-Practices, Fuzzing, Maintained and the like) describe
# the project on GitHub, not the tree. License is left out because local mode
# cannot see GitHub's license detection and reports MIT as unrecognized.
CHECKS = (
    "Binary-Artifacts",
    "Dangerous-Workflow",
    "Dependency-Update-Tool",
    "Packaging",
    "Pinned-Dependencies",
    "SAST",
    "Security-Policy",
    "Token-Permissions",
    "Vulnerabilities",
)

# Any warning in these fails, even at 10. Elsewhere a warning beside a 10 is
# informational (SAST warns about a branch it does not score).
STRICT = {
    "Binary-Artifacts",
    "Dangerous-Workflow",
    "Pinned-Dependencies",
    "Token-Permissions",
}

# Scorecard's score for a check that could not run.
ERRORED = -1


def failures(check: dict) -> list[str]:
    """Why a check fails, one line each; empty when it passes."""
    name, score = check["name"], check["score"]
    warnings = [d for d in check.get("details") or [] if d.startswith("Warn:")]
    if score == 10:
        return warnings if name in STRICT else []
    return [check.get("reason") or "no reason given", *warnings]


def main(path: str) -> int:
    with Path(path).open() as handle:
        report = json.load(handle)

    failed = []
    for check in report.get("checks", []):
        name, score = check["name"], check["score"]
        why = failures(check)
        shown = "errored" if score == ERRORED else f"{score}/10"
        print(f"  {name}: {shown}{' FAILED' if why else ''}")
        for line in why:
            print(f"      {line}")
        if why:
            failed.append(name)

    ran = {check["name"] for check in report.get("checks", [])}
    for missing in sorted(set(CHECKS) - ran):
        print(f"  {missing}: not reported FAILED")
        failed.append(missing)

    if failed:
        print(f"[security] Scorecard: {len(failed)} of {len(CHECKS)} checks failed: {', '.join(failed)}.")
        return 1
    print(f"[security] Scorecard: all {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--checks"]:
        print(",".join(CHECKS))
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1]))
