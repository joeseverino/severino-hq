"""Hold mise.lock to mise.toml: every pinned tool is locked for every platform a gate runs on."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The pipeline's runners and the machines the gates are run on by hand.
PLATFORMS = ("linux-x64", "macos-arm64")
# Read from another file, which is its pin; mise resolves it on install.
UNLOCKED = {"node"}


def missing(config: dict, lock: dict) -> list[str]:
    found = []
    for name, declared in config.get("tools", {}).items():
        if name in UNLOCKED:
            continue
        version = declared["version"] if isinstance(declared, dict) else declared
        entries = [entry for entry in lock.get("tools", {}).get(name, []) if entry.get("version") == version]
        for platform in PLATFORMS:
            if not any(entry.get(f"platforms.{platform}", {}).get("checksum") for entry in entries):
                found.append(f"{name}@{version} has no checksum for {platform}")
    return found


def main() -> int:
    config = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "mise.lock").read_text(encoding="utf-8"))
    found = missing(config, lock)
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print("Run: mise lock --platform linux-x64,linux-arm64,macos-arm64,macos-x64", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
