"""Hold mise.lock to mise.toml, and the images' uv to the uv mise pins.

Every pinned tool is locked for every platform a gate runs on. The images copy
uv from its own image, pinned by digest; its tag is the version mise.toml pins.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The pipeline's runners and the machines the gates are run on by hand.
PLATFORMS = ("linux-x64", "macos-arm64")
IMAGES = ("Dockerfile", "composition/Dockerfile")
UV_IMAGE = re.compile(r"^FROM ghcr\.io/astral-sh/uv:(\S+)@sha256:[0-9a-f]{64}\b", re.MULTILINE)


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


def image_drift(config: dict, images: dict[str, str]) -> list[str]:
    version = declared_version(config["tools"]["uv"])
    found = []
    for name, text in images.items():
        tags = UV_IMAGE.findall(text)
        if not tags:
            found.append(f"{name} copies no digest-pinned ghcr.io/astral-sh/uv image")
        found.extend(f"{name} takes uv {tag}; mise.toml pins {version}" for tag in tags if tag != version)
    return found


def main() -> int:
    config = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "mise.lock").read_text(encoding="utf-8"))
    images = {name: (ROOT / name).read_text(encoding="utf-8") for name in IMAGES}
    unlocked = missing(config, lock)
    drift = image_drift(config, images)
    for line in unlocked + drift:
        print(line, file=sys.stderr)
    if unlocked:
        print("Run: mise lock --platform linux-x64,linux-arm64,macos-arm64,macos-x64", file=sys.stderr)
    return 1 if unlocked or drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
