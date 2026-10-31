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
import os
import re
import shutil
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


def base_policy(policy: Path, base_sha: str, run: Run) -> str:
    """The policy file's digest at the pull request's base commit."""
    run(["git", "fetch", "--quiet", "--depth", "1", "origin", base_sha], check=True)
    shown = run(["git", "show", f"FETCH_HEAD:{policy.as_posix()}"], check=True, stdout=subprocess.PIPE)
    return sha256(shown.stdout)


def coordinated(path: Path) -> set[str]:
    """The repositories a coordinated change builds from a branch."""
    if not path.is_file():
        return set()
    return {line.split("=", 1)[0] for line in path.read_text().splitlines() if line}


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
                "node", str(cordon / "admission" / "verify.mjs"),
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


def verify(
    composition: dict, policy_sha: str, artifacts_dir: Path, cordon: Path, skip: set[str], run: Run
) -> tuple[list[str], list[Path]]:
    """Every admission not built from a branch: compose_plugins' arguments and the verified wheels."""
    # The policy is stated once, so a composition of no extensions still names it.
    arguments = ["--policy-sha256", policy_sha]
    wheels = []
    for extension in composition["extensions"]:
        if extension.get("repository") in skip:
            continue
        found = verify_one(extension, artifacts_dir / str(extension.get("id")), policy_sha, cordon, run)
        arguments += found
        wheels.append(Path(found[-1]))
    return arguments, wheels


def main(argv: Sequence[str] | None = None, run: Run = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--policy", type=Path, default=Path(POLICY))
    parser.add_argument("--set", type=Path, default=Path("composition-set.json"))
    parser.add_argument("--coordinated", type=Path, default=Path("coordinated.txt"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--cordon", type=Path, default=Path("cordon"))
    parser.add_argument("--out", type=Path, default=Path("build/compose-args"))
    parser.add_argument("--candidate-wheels", type=Path, default=Path("candidate-wheels"))
    arguments = parser.parse_args(argv)
    # A candidate (a pull request with coordinated branches) skips the extensions
    # built from a branch and verifies the rest under the base branch's policy
    # (BASE_SHA), which they were signed under, copying their wheels aside.
    candidate = os.environ.get("CANDIDATE") == "true"
    try:
        policy = arguments.policy
        policy_sha = base_policy(policy, os.environ["BASE_SHA"], run) if candidate else sha256(policy.read_bytes())
        skip = coordinated(arguments.coordinated) if candidate else set()
        composition = json.loads(arguments.set.read_text())
        compose_args, wheels = verify(composition, policy_sha, arguments.artifacts, arguments.cordon, skip, run)
    except Refused as refused:
        print(f"::error::{refused}")
        return 1
    except subprocess.CalledProcessError as failed:
        return failed.returncode or 1
    if candidate:
        arguments.candidate_wheels.mkdir(parents=True, exist_ok=True)
        for wheel in wheels:
            shutil.copy(wheel, arguments.candidate_wheels)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(" ".join(compose_args) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
