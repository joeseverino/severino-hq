"""Verify every extension's admission against the signer the composition set declares.

The host verifies each signature itself, against the identity
``composition-set.json`` declares, so an extension cannot widen who may sign for
it by editing its own repository. ``build/compose-args`` names every verified
entry and wheel for ``compose_plugins``. Stdlib only: its job sets up no toolchain.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

POLICY = "policy/plugin-admission-v1.json"
ISSUER = "https://token.actions.githubusercontent.com"
HOST = "severino-hq"
# The paths become words of the next step's command line, so each is held to
# a wheel's own characters (PEP 427): nothing a shell reads.
PLAIN_PATH = re.compile(r"[A-Za-z0-9._/+-]+")
WHEEL_NAME = re.compile(
    r"[A-Za-z0-9_.]+-[A-Za-z0-9_.+]+(-[0-9][A-Za-z0-9_.]*)?-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl"
)

Run = Callable[..., subprocess.CompletedProcess[bytes]]


class Refused(Exception):
    """An admission that cannot be composed; the message is a public annotation."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def first(directory: Path, pattern: str) -> Path | None:
    return next(iter(sorted(directory.rglob(pattern))), None) if directory.is_dir() else None


def artifacts(repository: str, directory: Path) -> tuple[Path, Path, Path]:
    """An admission's statement, bundle and wheel, each a plain file name."""
    found = [first(directory, name) for name in ("admission.json", "admission.sigstore.json", "*.whl")]
    statement, bundle, wheel = found
    if statement is None or bundle is None or wheel is None:
        raise Refused(f"{repository} is missing its admission statement, bundle or wheel")
    if not all(PLAIN_PATH.fullmatch(str(path)) for path in (statement, bundle, wheel)):
        raise Refused(f"{repository} ships an artifact whose name is not a plain file name")
    if not WHEEL_NAME.fullmatch(wheel.name):
        raise Refused(f"{repository} ships a wheel whose name is not a wheel's")
    return statement, bundle, wheel


def verify_one(extension: dict[str, str], directory: Path, policy_sha: str, cordon: Path, run: Run) -> list[str]:
    """Verify one admission; its compose_plugins arguments."""
    repository, workflow, plugin_id = (extension.get(key) for key in ("repository", "workflow", "id"))
    if not (repository and workflow and plugin_id):
        raise Refused("an extension in the composition set has no repository, workflow or id")
    statement, bundle, wheel = artifacts(repository, directory)
    # cordon names the field that differs but not the values; say both, so a
    # policy mismatch shows which side moved.
    signed = json.loads(statement.read_text()).get("policy", {}).get("sha256")
    if signed != policy_sha:
        raise Refused(f"{repository} was admitted under policy {signed}; this composition's policy is {policy_sha}")
    entry = directory / "verified-entry.json"
    with entry.open("wb") as out:
        run(
            [
                "node", str(cordon / "admission" / "verify.ts"),
                "--statement", str(statement),
                "--bundle", str(bundle),
                "--artifact", str(wheel),
                "--identity", f"https://github.com/{repository}/{workflow}@refs/heads/main",
                "--issuer", ISSUER,
                "--repository", repository,
                "--workflow", workflow,
                "--host", HOST,
                "--plugin-id", plugin_id,
                "--policy-sha", policy_sha,
            ],
            check=True,
            stdout=out,
        )  # fmt: skip
    return ["--entry", str(entry), "--wheel", str(wheel)]


def verify(composition: dict, policy_sha: str, artifacts_dir: Path, cordon: Path, run: Run) -> list[str]:
    """Every admission: compose_plugins' arguments."""
    # The policy is stated once, so a composition of no extensions still names it.
    arguments = ["--policy-sha256", policy_sha]
    for extension in composition["extensions"]:
        arguments += verify_one(extension, artifacts_dir / str(extension.get("id")), policy_sha, cordon, run)
    return arguments


def main(argv: Sequence[str] | None = None, run: Run = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--policy", type=Path, default=Path(POLICY))
    parser.add_argument("--set", type=Path, default=Path("composition-set.json"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--cordon", type=Path, default=Path("cordon"))
    parser.add_argument("--out", type=Path, default=Path("build/compose-args"))
    arguments = parser.parse_args(argv)
    try:
        policy_sha = sha256(arguments.policy.read_bytes())
        composition = json.loads(arguments.set.read_text())
        compose_args = verify(composition, policy_sha, arguments.artifacts, arguments.cordon, run)
    except Refused as refused:
        print(f"::error::{refused}")
        return 1
    except subprocess.CalledProcessError as failed:
        return failed.returncode or 1
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(" ".join(compose_args) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
