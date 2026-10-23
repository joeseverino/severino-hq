"""Print CodeQL alerts from a SARIF file, and exit non-zero if any survive.

    python -m tools.pipeline.codeql_report SARIF [--exclude RULE ...]

`scan:codeql` passes the rules `.github/codeql/codeql-config.yml` excludes,
read from it, so a local run and the Action agree on what counts as a
finding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hq.platform.application.ui import counted


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="codeql_report")
    parser.add_argument("sarif", type=Path)
    parser.add_argument("--exclude", action="append", default=[], metavar="RULE")
    args = parser.parse_args(argv)
    runs = json.loads(args.sarif.read_text()).get("runs") or []
    if not runs:
        # An analysis that ran writes a run even when it finds nothing.
        print("[security] CodeQL wrote no analysis run.", file=sys.stderr)
        return 1

    alerts = [result for run in runs for result in run.get("results", []) if result.get("ruleId") not in args.exclude]

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
        print(f"[security] CodeQL found {counted(len(alerts), 'alert')}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
