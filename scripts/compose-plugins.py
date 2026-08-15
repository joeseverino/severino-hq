#!/usr/bin/env python3
"""Assemble the build context for an image carrying several admitted plugins.

    compose-plugins.py --entry a.json --wheel a.whl \
                       --entry b.json --wheel b.whl \
                       --out build/composition

Each plugin is verified and admitted independently, producing a canonical
verified entry. Merging those entries into one lock is Cordon's job -- its lock
tool already takes repeated --entry, sorts by plugin id and rejects duplicates
-- so this shells out to it instead of reimplementing the lock format. A second
implementation could disagree with the one the runtime validates against, which
is the failure worth avoiding.

What remains here is genuinely host-side: pair each wheel with its entry, check
the bytes against the digest that entry admitted, and derive the enabled plugin
list from the merged lock so the enabled and approved sets cannot drift apart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

CORDON_LOCK = os.environ.get("CORDON_LOCK", "cordon-admission-lock")
HOST = "severino-hq"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", action="append", required=True, type=Path)
    parser.add_argument("--wheel", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    if len(args.entry) != len(args.wheel):
        parser.error("each --entry needs exactly one matching --wheel")

    command = [CORDON_LOCK, "--host", HOST]
    for entry in args.entry:
        command += ["--entry", str(entry)]
    merged = subprocess.run(command, capture_output=True, text=True)  # noqa: S603
    if merged.returncode != 0:
        print(merged.stderr.strip() or "cordon refused the composition", file=sys.stderr)
        return 1
    lock = json.loads(merged.stdout)

    by_distribution = {entry["distribution"]: entry for entry in lock["plugins"]}
    digests: list[str] = []
    for wheel in args.wheel:
        distribution = wheel.name.split("-")[0].replace("_", "-")
        approved = by_distribution.get(distribution)
        if approved is None:
            print(f"{wheel.name} has no entry in this composition", file=sys.stderr)
            return 1
        actual = sha256(wheel)
        if actual != approved["artifact_sha256"]:
            print(
                f"{wheel.name} does not match its admitted digest "
                f"(admitted {approved['artifact_sha256'][:12]}, built {actual[:12]})",
                file=sys.stderr,
            )
            return 1
        digests.append(f"{actual}  {wheel.name}")

    policies = {entry["policy_sha256"] for entry in lock["plugins"]}
    if len(policies) > 1:
        # The runtime compares every approval against one expected policy, so a
        # mixed set can never satisfy it. Fail here, with the reason.
        print(f"plugins were admitted under {len(policies)} policies", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob("*.whl"):
        stale.unlink()
    for wheel in args.wheel:
        shutil.copy2(wheel, args.out / wheel.name)
    (args.out / "plugin-lock.json").write_text(json.dumps(lock) + "\n")

    # Derived from the lock, never configured separately.
    references = ",".join(
        f"{entry['distribution'].replace('-', '_')}.plugin:plugin"
        for entry in lock["plugins"]
    )
    print(f"composed {len(lock['plugins'])} plugin(s): {references}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"references={references}\n")
            handle.write(f"policy_sha256={policies.pop()}\n")
            handle.write("digests<<EOF\n" + "\n".join(digests) + "\nEOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
