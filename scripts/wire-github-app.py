#!/usr/bin/env python3
"""Put HQ's GitHub App key where the pipeline reads it.

    scripts/wire-github-app.py --vault VAULT --item ITEM --client-id ID \\
        --extension OWNER/REPO [--extension OWNER/REPO ...]

Reads the app's private key from 1Password and sets, through standard input so
it never touches a file or a command line:

- on the host repository: the HQ_APP_KEY secret and HQ_APP_CLIENT_ID variable,
  which the composition uses to read the extensions' admissions
- on each extension: an ``admission`` environment limited to main, holding
  HQ_APP_KEY, and the HQ_APP_CLIENT_ID variable, which its admission uses to
  start the host's composition

Run it again after rotating the key. Everything personal is an argument;
nothing personal is in this repository. Needs `gh` signed in as the owner of
the repositories and `op` signed in to the vault.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

ENVIRONMENT = "admission"


def run(command: list[str], *, stdin: str | None = None) -> str:
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        # Never echo stdin: it is a private key.
        sys.exit(f"{' '.join(command[:3])} failed: {done.stderr.strip()}")
    return done.stdout


def private_key(vault: str, item: str) -> str:
    """The key from a Document item, or an SSH Key item's private key."""

    found = subprocess.run(["op", "document", "get", item, "--vault", vault], capture_output=True, text=True, check=False)
    key = found.stdout if found.returncode == 0 else run(["op", "read", f"op://{vault}/{item}/private key"])
    if "PRIVATE KEY-----" not in key:
        sys.exit(f"{item} in {vault} holds no private key.")
    return key


def host_repository() -> str:
    return run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()


def environment(repository: str) -> None:
    """``admission``, deployable from main alone, so no branch reads its secret."""

    run([
        "gh", "api", "-X", "PUT", f"repos/{repository}/environments/{ENVIRONMENT}",
        "-F", "deployment_branch_policy[protected_branches]=false",
        "-F", "deployment_branch_policy[custom_branch_policies]=true",
    ])
    policies = json.loads(run(["gh", "api", f"repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies"]))
    if not any(policy.get("name") == "main" for policy in policies.get("branch_policies") or ()):
        run([
            "gh", "api", "-X", "POST", f"repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies",
            "-f", "name=main", "-f", "type=branch",
        ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--item", required=True, help="The 1Password item holding the app's private key.")
    parser.add_argument("--client-id", required=True, help="The app's client ID, from its settings page.")
    parser.add_argument("--extension", action="append", required=True, help="OWNER/REPO, once per extension.")
    args = parser.parse_args()

    key = private_key(args.vault, args.item)
    host = host_repository()
    run(["gh", "secret", "set", "HQ_APP_KEY", "--repo", host], stdin=key)
    run(["gh", "variable", "set", "HQ_APP_CLIENT_ID", "--repo", host, "--body", args.client_id])
    print(f"{host}: HQ_APP_KEY and HQ_APP_CLIENT_ID set.")
    for repository in args.extension:
        environment(repository)
        run(["gh", "secret", "set", "HQ_APP_KEY", "--repo", repository, "--env", ENVIRONMENT], stdin=key)
        run(["gh", "variable", "set", "HQ_APP_CLIENT_ID", "--repo", repository, "--body", args.client_id])
        print(f"{repository}: {ENVIRONMENT} environment (main only) with HQ_APP_KEY; HQ_APP_CLIENT_ID set.")


if __name__ == "__main__":
    main()
