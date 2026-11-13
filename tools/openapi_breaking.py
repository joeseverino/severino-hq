"""lint:openapi-breaking: a change to the machine API's document that breaks a client fails, unless
the exemption file admits that change with a reason.

oasdiff compares the document with the base's copy and judges each change by its own checks; the
exemption file is its ``--err-ignore`` file, which it reads line by line. This module holds every
line oasdiff could read as an entry to carry its reason before oasdiff runs.

    python -m tools.openapi_breaking [base]

The base is fast's: the merge base with the ref named, or else with the branch HEAD lands on.
"""

import subprocess
import sys
from collections.abc import Iterable

from tools.gate.fast import ROOT, merge_base

DOCUMENT = "hq/platform/api/hq-api.openapi.json"
EXEMPTIONS = "hq/platform/api/hq-api.breaking-exemptions.md"
REASON = "Reason:"


def unexplained(lines: Iterable[str]) -> list[int]:
    """The numbers of the lines oasdiff could match to a change that give no reason after ``Reason:``.

    oasdiff (checker/*_change.go, MatchIgnore) matches a line, lowercased, to an operation's change
    by a field starting with a slash, and to a shared schema's or a security scheme's change by the
    word components or security in it; any other line matches nothing."""

    found = []
    for number, line in enumerate(lines, start=1):
        lowered = line.lower()
        matchable = any(field.startswith("/") for field in lowered.split()) or any(
            word in lowered for word in ("components", "security")
        )
        if matchable and not line.partition(REASON)[2].strip():
            found.append(number)
    return found


def main(argv: list[str]) -> int:
    lines = (ROOT / EXEMPTIONS).read_text().splitlines()
    if missing := unexplained(lines):
        for number in missing:
            print(f"{EXEMPTIONS}:{number}: an exemption says why clients can take it, after {REASON}", file=sys.stderr)
        return 1
    base = merge_base(argv[0] if argv and argv[0] else None)
    print(f"lint:openapi-breaking: {DOCUMENT} against {base[:9]}", flush=True)
    command = ["oasdiff", "breaking", "--fail-on", "ERR", "--format", "singleline", "--err-ignore", EXEMPTIONS]
    judged = subprocess.run([*command, f"{base}:{DOCUMENT}", DOCUMENT], cwd=ROOT, check=False)
    if judged.returncode:
        print(
            "lint:openapi-breaking: each error above breaks a client of /api/v2/. Keep the old shape, or admit"
            f" the break: copy its line into {EXEMPTIONS} and add {REASON} and why clients can take it.",
            file=sys.stderr,
        )
    return judged.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
