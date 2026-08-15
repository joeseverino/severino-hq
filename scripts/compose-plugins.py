#!/usr/bin/env python3
"""Assemble a build context for the composed image from admitted extensions.

    compose-plugins.py --admission a.json --wheel a.whl \
                       --admission b.json --wheel b.whl \
                       --out build/composition

Takes one signed admission plus its wheel per extension, merges the approvals
into a single lock, and writes the build context the composition Dockerfile
expects. Verifying the signatures is CI's job and happens before this runs;
this owns the part that decides whether a set of already-verified approvals may
travel together, and records the digests the image build re-checks.

Emits GitHub Actions outputs so the workflow never restates the plugin list --
the enabled set is derived from the lock, because the runtime refuses to boot
if the two disagree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.composition import (  # noqa: E402
    CompositionError,
    composition_plugin_references,
    merge_admissions,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission", action="append", required=True, type=Path)
    parser.add_argument("--wheel", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    if len(args.admission) != len(args.wheel):
        parser.error("each --admission needs exactly one matching --wheel")

    approvals = [json.loads(path.read_text()) for path in args.admission]
    try:
        lock = merge_admissions(approvals)
    except CompositionError as error:
        print(f"composition refused: {error}", file=sys.stderr)
        return 1

    # Each wheel must match the digest its own admission recorded, so a wheel
    # swapped after signing is caught here rather than shipped.
    by_distribution = {entry["distribution"]: entry for entry in lock["plugins"]}
    digests: list[str] = []
    for wheel in args.wheel:
        distribution = wheel.name.split("-")[0].replace("_", "-")
        approval = by_distribution.get(distribution)
        if approval is None:
            print(f"{wheel.name} has no approval in this composition", file=sys.stderr)
            return 1
        actual = sha256(wheel)
        if actual != approval["artifact_sha256"]:
            print(
                f"{wheel.name} does not match its admitted digest "
                f"(admitted {approval['artifact_sha256'][:12]}…, built {actual[:12]}…)",
                file=sys.stderr,
            )
            return 1
        digests.append(f"{actual}  {wheel.name}")

    args.out.mkdir(parents=True, exist_ok=True)
    for wheel in args.wheel:
        shutil.copy2(wheel, args.out / wheel.name)
    (args.out / "plugin-lock.json").write_text(json.dumps(lock, indent=2) + "\n")

    references = composition_plugin_references(lock)
    policy = lock["plugins"][0]["policy_sha256"]

    print(f"composed {len(lock['plugins'])} plugin(s): {references}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"references={references}\n")
            handle.write(f"policy_sha256={policy}\n")
            handle.write("digests<<EOF\n" + "\n".join(digests) + "\nEOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
