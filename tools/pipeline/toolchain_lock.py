"""Hold mise.lock to mise.toml.

Every pinned tool is locked for each of mise.toml's lockfile_platforms, and
the images install their build tools from the same lock. An image takes
nothing from another image but the Python it runs on and mise from its own
image, by digest, at the release min_version names, so no build tool has a
second pin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ("Dockerfile", "composition/Dockerfile")
# The OS and interpreter the app runs on: a base, not a build tool.
BASE_IMAGE = "python:"
# The installer of every other build tool. Renovate moves its tag and digest
# and min_version together.
MISE_IMAGE = "ghcr.io/jdx/mise"
MISE_PINNED = re.compile(r"ghcr\.io/jdx/mise:(?P<tag>[^@\s]+)@sha256:[0-9a-f]{64}")
STAGE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.MULTILINE | re.IGNORECASE)
COPY_FROM = re.compile(r"--from=(\S+)")


def pinned() -> dict[str, str]:
    """Each tool mise.toml pins, at the version mise renders for it (Go's is read from go.mod).
    Unlocked, so mise reports a pin mise.lock lacks rather than dropping it."""
    listed = subprocess.run(
        ["mise", "ls", "--current", "--json", "--quiet"],
        cwd=ROOT,
        env={**os.environ, "MISE_LOCKED": "0"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    config = str(ROOT / "mise.toml")
    return {
        name: entry["requested_version"]
        for name, entries in json.loads(listed).items()
        for entry in entries
        if entry.get("source", {}).get("path") == config
    }


def missing(platforms: list[str], tools: dict[str, str], lock: dict) -> list[str]:
    found = []
    for name, version in tools.items():
        entries = [entry for entry in lock.get("tools", {}).get(name, []) if entry.get("version") == version]
        found.extend(
            f"{name}@{version} has no checksum for {platform}"
            for platform in platforms
            if not any(entry.get(f"platforms.{platform}", {}).get("checksum") for entry in entries)
        )
    return found


def foreign_image(ref: str, mise: str) -> str | None:
    pinned = MISE_PINNED.fullmatch(ref)
    if pinned and pinned["tag"] == mise:
        return None
    if ref.startswith(MISE_IMAGE):
        return f"takes {ref}; mise comes from {MISE_IMAGE}:{mise}, the min_version release, by digest"
    return f"takes {ref} from an image; a build tool installs from mise.lock"


def foreign_images(images: dict[str, str], mise: str) -> list[str]:
    found = []
    for name, text in images.items():
        stages = STAGE.findall(text)
        own = {alias for _, alias in stages if alias}
        refs = [ref for ref, _ in stages] + COPY_FROM.findall(text)
        problems = [
            foreign_image(ref, mise) for ref in refs if ref not in own and not ref.startswith(("$", BASE_IMAGE))
        ]
        found.extend(f"{name} {problem}" for problem in problems if problem)
    return found


def main() -> int:
    config = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "mise.lock").read_text(encoding="utf-8"))
    tools = pinned()
    unlocked = missing(config["settings"]["lockfile_platforms"], tools, lock)
    images = {name: (ROOT / name).read_text(encoding="utf-8") for name in IMAGES}
    drift = foreign_images(images, config["min_version"])
    drift += [f"mise resolves {name} from another config than mise.toml" for name in config["tools"].keys() - tools]
    for line in unlocked + drift:
        print(line, file=sys.stderr)
    if unlocked:
        print("Run: mise lock", file=sys.stderr)
    return 1 if unlocked or drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
