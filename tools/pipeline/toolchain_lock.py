"""Hold the images and mise.toml to the one toolchain mise.lock records.

`mise lock --check` holds mise.lock to mise.toml (lint:toolchain). The
images install their build tools from the same lock: an image takes nothing
from another image but the Python it runs on and mise from its own image, by
digest, at the release min_version names, so no build tool has a second pin.
hk.pkl names hk's release in the schema it amends, which hk's configuration
has no versionless form of, so that copy is held to mise.toml's pin.
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
# The reserved name for an empty base, which pulls nothing: a stage of it only
# holds what it copies from the stages beside it.
EMPTY_BASE = "scratch"
# The installer of every other build tool. Renovate moves its tag and digest
# and min_version together.
MISE_IMAGE = "ghcr.io/jdx/mise"
MISE_PINNED = re.compile(r"ghcr\.io/jdx/mise:(?P<tag>[^@\s]+)@sha256:[0-9a-f]{64}")
STAGE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.MULTILINE | re.IGNORECASE)
COPY_FROM = re.compile(r"--from=(\S+)")
HK = "aqua:jdx/hk"
HK_SCHEMA = re.compile(r"package://github\.com/jdx/hk/releases/download/v([^/]+)/hk@([^#]+)#")


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
        own = {alias for _, alias in stages if alias} | {EMPTY_BASE}
        refs = [ref for ref, _ in stages] + COPY_FROM.findall(text)
        problems = [
            foreign_image(ref, mise) for ref in refs if ref not in own and not ref.startswith(("$", BASE_IMAGE))
        ]
        found.extend(f"{name} {problem}" for problem in problems if problem)
    return found


def schema_drift(hk_pkl: str, hk: str) -> list[str]:
    releases = {release for pair in HK_SCHEMA.findall(hk_pkl) for release in pair}
    return [f"hk.pkl amends hk {release}; mise.toml pins {hk}" for release in sorted(releases - {hk})]


def main() -> int:
    config = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    tools = pinned()
    images = {name: (ROOT / name).read_text(encoding="utf-8") for name in IMAGES}
    drift = foreign_images(images, config["min_version"])
    drift += schema_drift((ROOT / "hk.pkl").read_text(encoding="utf-8"), tools[HK])
    drift += [f"mise resolves {name} from another config than mise.toml" for name in config["tools"].keys() - tools]
    for line in drift:
        print(line, file=sys.stderr)
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
