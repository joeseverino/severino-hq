"""Print CodeQL alerts from a SARIF file, and exit non-zero if any survive.

The filter is the one in `.github/codeql/codeql-config.yml`, applied again
here so a local run and the Action agree on what counts as a finding. A
rule excluded there and reported here would make this script fail on
something CI is content with, which is worse than not running it.
"""

from __future__ import annotations

import json
import sys

# Kept in step with `query-filters` in .github/codeql/codeql-config.yml.
EXCLUDED = {"py/cyclic-import"}


def main(path: str) -> int:
    with open(path) as handle:
        sarif = json.load(handle)

    alerts = [
        result
        for run in sarif.get("runs", [])
        for result in run.get("results", [])
        if result.get("ruleId") not in EXCLUDED
    ]

    for alert in alerts:
        location = alert.get("locations", [{}])[0].get("physicalLocation", {})
        print(
            "  {}  {}:{}  {}".format(
                alert.get("ruleId", "?"),
                location.get("artifactLocation", {}).get("uri", "?"),
                location.get("region", {}).get("startLine", "?"),
                alert.get("message", {}).get("text", "").replace("\n", " ")[:96],
            )
        )

    if alerts:
        print(f"[security] CodeQL found {len(alerts)} alert(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
