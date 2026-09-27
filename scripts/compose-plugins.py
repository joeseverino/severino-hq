#!/usr/bin/env python3
"""Assemble the build context for an image carrying several admitted plugins.

    compose-plugins.py --entry a.json --wheel a.whl \
                       --entry b.json --wheel b.whl \
                       --out build/composition
    compose-plugins.py --candidate --wheel a.whl --wheel b.whl --out build/candidate

Each plugin is verified and admitted independently, producing a canonical
verified entry. Merging those entries into one lock is Cordon's job (its lock
tool already takes repeated --entry, sorts by plugin id and rejects duplicates)
so this shells out to it instead of reimplementing the lock format. A second
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

# Run as a file, so the repository root is not on the path by itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from application.ui import counted  # noqa: E402

CORDON_LOCK = os.environ.get("CORDON_LOCK", "cordon-admission-lock")
HOST = "severino-hq"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def distribution_of(wheel: Path) -> str:
    return wheel.name.split("-")[0].replace("_", "-")


def reference_of(distribution: str) -> str:
    """The manifest reference an extension's distribution declares."""
    return f"{distribution.replace('-', '_')}.plugin:plugin"


def stage(wheels: list[Path], out: Path) -> list[str]:
    """Copy the wheels into the build context; their ``<sha256>  <name>`` lines."""
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.whl"):
        stale.unlink()
    for wheel in wheels:
        shutil.copy2(wheel, out / wheel.name)
    return [f"{sha256(wheel)}  {wheel.name}" for wheel in wheels]


def emit(references: str, digests: list[str], policy_sha256: str | None = None) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"references={references}\n")
            if policy_sha256:
                handle.write(f"policy_sha256={policy_sha256}\n")
            handle.write("digests<<EOF\n" + "\n".join(digests) + "\nEOF\n")


def compose_admitted(entries: list[Path], wheels: list[Path], out: Path) -> int:
    command = [CORDON_LOCK, "--host", HOST]
    for entry in entries:
        command += ["--entry", str(entry)]
    merged = subprocess.run(command, capture_output=True, text=True)  # noqa: S603
    if merged.returncode != 0:
        print(merged.stderr.strip() or "cordon refused the composition", file=sys.stderr)
        return 1
    lock = json.loads(merged.stdout)

    by_distribution = {entry["distribution"]: entry for entry in lock["plugins"]}
    for wheel in wheels:
        approved = by_distribution.get(distribution_of(wheel))
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

    policies = {entry["policy_sha256"] for entry in lock["plugins"]}
    if len(policies) > 1:
        # The runtime compares every approval against one expected policy, so a
        # mixed set can never satisfy it. Fail here, with the reason.
        print(f"plugins were admitted under {len(policies)} policies", file=sys.stderr)
        return 1

    digests = stage(wheels, out)
    (out / "plugin-lock.json").write_text(json.dumps(lock) + "\n")

    # Derived from the lock, never configured separately.
    references = ",".join(reference_of(entry["distribution"]) for entry in lock["plugins"])
    print(f"composed {counted(len(lock['plugins']), 'plugin')}: {references}")
    emit(references, digests, policies.pop())
    return 0


def compose_candidate(wheels: list[Path], out: Path) -> int:
    """Stage wheels for a verify-only image: no lock, so it can never be admitted."""
    distributions = sorted(distribution_of(wheel) for wheel in wheels)
    if len(set(distributions)) != len(distributions):
        print("a distribution appears more than once in the candidate", file=sys.stderr)
        return 1
    if (out / "plugin-lock.json").exists():
        (out / "plugin-lock.json").unlink()
    digests = stage(wheels, out)
    references = ",".join(reference_of(distribution) for distribution in distributions)
    print(f"candidate {counted(len(wheels), 'plugin')}: {references}")
    emit(references, digests)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", action="append", default=[], type=Path)
    parser.add_argument("--wheel", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="stage unadmitted wheels for a verify-only image; writes no lock",
    )
    args = parser.parse_args(argv)

    if args.candidate:
        if args.entry:
            parser.error("--candidate takes wheels only")
        return compose_candidate(args.wheel, args.out)
    if len(args.entry) != len(args.wheel):
        parser.error("each --entry needs exactly one matching --wheel")
    return compose_admitted(args.entry, args.wheel, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
