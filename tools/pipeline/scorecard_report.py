"""Judge a local OpenSSF Scorecard run, and exit non-zero on any finding.

Reads the JSON that `scorecard --local <tree> --show-details --format json`
writes. Every check in CHECKS must score 10 and the STRICT ones must carry no
warning.
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


def main(path: str) -> int:
    with Path(path).open() as handle:
        report = json.load(handle)

    failed = 0
    for check in report.get("checks", []):
        name, score = check["name"], check["score"]
        warnings = [d for d in check.get("details") or [] if d.startswith("Warn:")]
        ok = score == 10 and not (name in STRICT and warnings)
        if score == 10 and name not in STRICT:
            warnings = []
        print(f"  {name}: {score}/10")
        for detail in warnings:
            print(f"      {detail}")
        if not ok:
            if not warnings:
                print(f"      {check.get('reason', 'no reason given')}")
            failed += 1

    ran = {check["name"] for check in report.get("checks", [])}
    for missing in sorted(set(CHECKS) - ran):
        print(f"  {missing}: not reported")
        failed += 1

    if failed:
        print(f"[security] Scorecard: {failed} of {len(CHECKS)} checks failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--checks"]:
        print(",".join(CHECKS))
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1]))
