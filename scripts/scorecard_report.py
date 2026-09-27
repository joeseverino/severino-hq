"""Judge a local OpenSSF Scorecard run, and exit non-zero on any finding.

Reads the JSON that `scorecard --local <tree> --show-details --format json`
writes. Every check in CHECKS must score 10 and the STRICT ones must carry no
warning, apart from findings listed in ACCEPTED, each of which names the exact
line it excuses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The checks local mode can answer from files alone. The rest (Branch-Protection,
# Code-Review, CII-Best-Practices, Fuzzing, Maintained and the like) describe
# the project on GitHub, not the tree. License is left out because local mode
# cannot see GitHub's license detection and reports MIT as unrecognised.
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

# Any warning in these fails, whatever the score. Elsewhere a warning can be
# informational (SAST warns about a branch it does not score), so the score
# decides.
STRICT = {
    "Binary-Artifacts",
    "Dangerous-Workflow",
    "Pinned-Dependencies",
    "Token-Permissions",
}

# (check, path) -> the exact source line the finding may point at. The image
# composed onto is always a digest reference, resolved in compose.yml, which
# Scorecard cannot see through a build argument. Any other line in that file,
# or this line anywhere else, still fails.
ACCEPTED = {
    ("Pinned-Dependencies", "composition/Dockerfile"): "FROM ${HQ_IMAGE}",
}

LOCATION = re.compile(r": (?P<path>[^\s:]+):(?P<line>\d+)$")


def accepted(check: str, detail: str, tree: Path) -> bool:
    match = LOCATION.search(detail)
    if not match:
        return False
    expected = ACCEPTED.get((check, match["path"]))
    if expected is None:
        return False
    lines = (tree / match["path"]).read_text().splitlines()
    index = int(match["line"]) - 1
    return 0 <= index < len(lines) and lines[index].strip() == expected


def main(path: str, tree: str) -> int:
    with open(path) as handle:
        report = json.load(handle)

    failed = 0
    for check in report.get("checks", []):
        name, score = check["name"], check["score"]
        warnings = [d for d in check.get("details") or [] if d.startswith("Warn:")]
        open_ = [d for d in warnings if not accepted(name, d, Path(tree))]
        excused = len(warnings) - len(open_)
        if name in STRICT:
            ok = not open_ and score >= 0
        else:
            ok = score == 10 or (not open_ and score >= 0)
            open_ = [] if score == 10 else open_
        note = f" ({excused} accepted)" if excused else ""
        print(f"  {name}: {score}/10{note}")
        for detail in open_:
            print(f"      {detail}")
        if not ok:
            if not open_:
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
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
