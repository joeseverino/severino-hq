"""The advisories mise audit finds against the tools mise.toml pins, less those
osv-scanner.toml ignores until their expiry, so the one file osv-scanner reads
beside the host's locks says what is ignored for mise.lock too. mise audit has
no ignore list of its own. Exits 1 on any advisory left.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def ignored(config: dict[str, Any], today: datetime.date) -> set[str]:
    """The ids an osv-scanner config ignores on the day given: an entry holds while its effectiveUntil is ahead."""
    entries = config.get("IgnoredVulns", [])
    return {entry["id"] for entry in entries if "effectiveUntil" not in entry or entry["effectiveUntil"] > today}


def unignored(report: dict[str, Any], skip: set[str]) -> list[dict[str, Any]]:
    """The advisories of a `mise audit --json` report that no id or alias of theirs ignores."""
    return [found for found in report["vulnerabilities"] if not skip & {found["id"], *found["aliases"]}]


def mise(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["mise", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def main() -> int:
    # The tools the repository's own configs pin, so the machine's global tools stay out.
    tools = mise("ls", "--local", "--json")
    audit = mise("audit", "--json", *json.loads(tools.stdout)) if tools.returncode == 0 else tools
    if audit.returncode not in (0, 1):
        sys.stderr.write(audit.stderr)
        return 2
    today = datetime.datetime.now(datetime.UTC).date()
    left = unignored(json.loads(audit.stdout), ignored(tomllib.loads((ROOT / "osv-scanner.toml").read_text()), today))
    for found in left:
        print(f"{found['tool']}@{found['version']} {found['id']}: {found['summary']} ({found['url']})")
    print(f"mise audit: {len(left)} advisories against the pinned tools")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
