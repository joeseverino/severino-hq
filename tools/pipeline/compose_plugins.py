"""Assemble the build context for an image carrying several admitted plugins.

Each wheel is paired with its admitted entry and checked against the digest that
entry admitted, and the enabled plugin list is derived from the merged lock, so
the enabled and approved sets cannot drift apart. ``--candidate`` stages
unadmitted wheels for a verify-only image instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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


def emit(out: Path, references: str, digests: list[str], policy_sha256: str | None = None) -> None:
    """Write the build arguments beside the wheels, not to step outputs.

    A step's env block is printed in its public log, and these name every
    extension, its version and its digest.
    """
    (out / "references").write_text(references + "\n")
    (out / "digests").write_text("\n".join(digests) + "\n")
    policy = out / "policy-sha256"
    if policy_sha256:
        policy.write_text(policy_sha256 + "\n")
    elif policy.exists():
        policy.unlink()


def compose_none(out: Path, policy_sha256: str) -> int:
    """The host with no extensions: an empty lock the runtime accepts as one, written
    here because there is nothing for Cordon to merge."""
    stage([], out)
    (out / "plugin-lock.json").write_text(json.dumps({"schema_version": 1, "host": HOST, "plugins": []}) + "\n")
    emit(out, "", [], policy_sha256)
    print("composition staged: no extensions")
    return 0


def compose_admitted(entries: list[Path], wheels: list[Path], out: Path, policy_sha256: str | None = None) -> int:
    if not entries:
        if not re.fullmatch(r"[0-9a-f]{64}", policy_sha256 or ""):
            print("a composition of no extensions must state its policy", file=sys.stderr)
            return 1
        return compose_none(out, policy_sha256)
    # Merging the entries into one lock is Cordon's (its lock tool sorts by plugin id
    # and rejects duplicates): a second implementation of the lock format could
    # disagree with the one the runtime validates against.
    command = [CORDON_LOCK, "--host", HOST]
    for entry in entries:
        command += ["--entry", str(entry)]
    merged = subprocess.run(command, capture_output=True, text=True, check=False)
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
    if policy_sha256 and policies != {policy_sha256}:
        print("plugins were admitted under another policy than this composition's", file=sys.stderr)
        return 1

    digests = stage(wheels, out)
    (out / "plugin-lock.json").write_text(json.dumps(lock) + "\n")

    # Derived from the lock, never configured separately.
    references = ",".join(reference_of(entry["distribution"]) for entry in lock["plugins"])
    emit(out, references, digests, policies.pop())
    print("composition staged")
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
    emit(out, references, digests)
    print("candidate staged")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", action="append", default=[], type=Path)
    parser.add_argument("--wheel", action="append", default=[], type=Path)
    parser.add_argument("--policy-sha256", help="the admission policy this composition is checked against")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="stage unadmitted wheels for a verify-only image; writes no lock",
    )
    args = parser.parse_args(argv)

    if args.candidate:
        if args.entry or args.policy_sha256:
            parser.error("--candidate takes wheels only")
        if not args.wheel:
            parser.error("--candidate needs at least one --wheel")
        return compose_candidate(args.wheel, args.out)
    if len(args.entry) != len(args.wheel):
        parser.error("each --entry needs exactly one matching --wheel")
    return compose_admitted(args.entry, args.wheel, args.out, args.policy_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
