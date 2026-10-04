"""Read dependency pins and bootstrap hashes from the project's one lock."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tool_pin(name: str, root: Path = ROOT) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    normalized = name.lower().replace("_", "-")
    found = []
    for declaration in project["dependency-groups"]["tools"] + project["dependency-groups"]["browser"]:
        match = re.match(r"([A-Za-z0-9_.-]+)(.*)", declaration)
        if match and match[1].lower().replace("_", "-") == normalized:
            version = re.fullmatch(r"==([^; ,]+)", match[2])
            if not version:
                raise ValueError(f"expected one exact tool pin for {name}")
            found.append(version[1])
    if len(found) != 1:
        raise ValueError(f"expected one exact tool pin for {name}")
    return found[0]


def uv_requirements(root: Path = ROOT) -> str:
    version = tool_pin("uv", root)
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == "uv" and package["version"] == version]
    if len(packages) != 1 or packages[0].get("source", {}).get("registry") != "https://pypi.org/simple":
        raise ValueError("uv bootstrap must have one pinned PyPI lock entry")
    package = packages[0]
    artifacts = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
    hashes = sorted({artifact["hash"] for artifact in artifacts})
    if not hashes or any(not re.fullmatch(r"sha256:[a-f0-9]{64}", digest) for digest in hashes):
        raise ValueError("uv bootstrap artifacts must carry SHA-256 hashes")
    return "uv==" + version + " " + " ".join("--hash=" + digest for digest in hashes) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pin", "uv-requirements"))
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    if args.command == "pin":
        if not args.name:
            parser.error("pin requires a tool name")
        print(tool_pin(args.name))
    else:
        print(uv_requirements(), end="")


if __name__ == "__main__":
    main()
