"""Hold mise.lock to mise.toml, and mise's Go to the language go.mod names.

Every pinned tool is locked for every platform a gate runs on, and the images
install their build tools from the same lock. The controller
builds with mise's Go, a release of the language version go.mod names. An
image takes nothing from another image but the Python it runs on and mise from
its own image, by digest, at the release min_version names, so no build tool
has a second pin.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The pipeline's runners, which also build the images, and the machines the
# gates are run on by hand.
PLATFORMS = ("linux-x64", "macos-arm64")
IMAGES = ("Dockerfile", "composition/Dockerfile")
# The OS and interpreter the app runs on: a base, not a build tool.
BASE_IMAGE = "python:"
# The installer of every other build tool. Renovate moves its tag and digest
# and min_version together.
MISE_IMAGE = "ghcr.io/jdx/mise"
MISE_PINNED = re.compile(r"ghcr\.io/jdx/mise:(?P<tag>[^@\s]+)@sha256:[0-9a-f]{64}")
STAGE = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.MULTILINE | re.IGNORECASE)
COPY_FROM = re.compile(r"--from=(\S+)")
GO_MOD = "controller/go.mod"
GO_DIRECTIVE = re.compile(r"^go (\S+)$", re.MULTILINE)
GO_TOOLCHAIN = re.compile(r"^toolchain go(\S+)$", re.MULTILINE)


def declared_version(declared: str | dict) -> str:
    return declared["version"] if isinstance(declared, dict) else declared


def missing(config: dict, lock: dict) -> list[str]:
    found = []
    for name, declared in config.get("tools", {}).items():
        version = declared_version(declared)
        entries = [entry for entry in lock.get("tools", {}).get(name, []) if entry.get("version") == version]
        found.extend(
            f"{name}@{version} has no checksum for {platform}"
            for platform in PLATFORMS
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


def go_drift(config: dict, go_mod: str) -> list[str]:
    version = declared_version(config["tools"]["go"])
    found = []
    language = GO_DIRECTIVE.search(go_mod)
    want = language.group(1) if language else "no version"
    if version != want and not version.startswith(want + "."):
        found.append(f"mise.toml pins Go {version}; {GO_MOD} names go {want}")
    toolchain = GO_TOOLCHAIN.search(go_mod)
    if toolchain and toolchain.group(1) != version:
        found.append(f"mise.toml pins Go {version}; {GO_MOD} names toolchain go{toolchain.group(1)}")
    return found


def main() -> int:
    config = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "mise.lock").read_text(encoding="utf-8"))
    unlocked = missing(config, lock)
    images = {name: (ROOT / name).read_text(encoding="utf-8") for name in IMAGES}
    drift = go_drift(config, (ROOT / GO_MOD).read_text(encoding="utf-8"))
    drift += foreign_images(images, config["min_version"])
    for line in unlocked + drift:
        print(line, file=sys.stderr)
    if unlocked:
        print("Run: mise lock --platform linux-x64,linux-arm64,macos-arm64,macos-x64", file=sys.stderr)
    return 1 if unlocked or drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
