"""Every reason HQ would refuse to adopt a repository of playbooks, as a check
of that repository's own commit: the automation worker's scan
(``hq_runner.catalog.read``) and HQ's judgment of it
(``automation_adoption.refusals``), the code adoption runs, so a commit HQ
would refuse fails the repository's CI before it merges.

`tools/pipeline/check-playbooks.sh <checkout>` runs it in the worker's
environment, from a repository's CI or a checkout of HQ (docs/Architecture.md,
"Checking a repository of playbooks").
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

lazy import django

lazy from hq.domains.control_plane.bridge_contract import departs
lazy from hq.platform.application.automation_adoption import refusals
lazy from hq_runner import ansible, catalog, procedures
lazy from hq_runner.ansible import Modules, RoleSpec
lazy from hq_runner.claim import Refused

# The settings a check needs, as the gates' throwaway ones (mise.toml's
# ``django`` template): no database is read, so nothing is a secret.
_SETTINGS = {
    "DJANGO_SETTINGS_MODULE": "hq.config.settings",
    "DJANGO_SECRET_KEY": "playbook-check-only-key-not-a-real-secret",
    "DJANGO_ALLOWED_HOSTS": "localhost",
    "SEVERINO_SANDBOX": "1",
}
# The repository a checkout is reported as: the bridge contract asks for one,
# and nothing judged reads it.
_REPOSITORY = "example/playbooks"


def _git(checkout: Path, *arguments: str, given: str = "") -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        input=given,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def checkout(directory: Path) -> procedures.Tip:
    """``directory``'s repository as its next commit would hold it: each file
    git would commit, tracked or not, by the object id of its content now."""

    root = Path(_git(directory, "rev-parse", "--show-toplevel").strip()).resolve()
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    paths = sorted(path for path in set(listed) if path and (root / path).is_file())
    objects = _git(root, "hash-object", "--stdin-paths", given="".join(f"{path}\n" for path in paths)).split()
    branch = _git(root, "branch", "--show-current").strip()
    revision = _git(root, "rev-parse", "HEAD").strip()
    return procedures.Tip(branch, revision, root, tuple(zip(paths, objects, strict=True)))


def check(tip: procedures.Tip, *, role_spec: RoleSpec, modules: Modules) -> tuple[str, ...]:
    """Every reason HQ would refuse ``tip`` or one of its playbooks."""

    try:
        found = {
            "repository": _REPOSITORY,
            **catalog.read(tip, role_spec=role_spec, modules=modules),
            # GitHub says it of a pushed commit alone, so ``refusals`` reads none.
            "verification": {"verified": False, "reason": "unavailable", "committer": ""},
            "reaches": [],
        }
    except Refused as refused:
        return (refused.message,)
    departed = departs("SourceCatalog", found)
    return (f"The scan departs from the bridge contract: {departed}",) if departed else refusals(found)


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: check-playbooks.sh <checkout>", file=sys.stderr)
        return 2
    for name, value in _SETTINGS.items():
        os.environ.setdefault(name, value)
    django.setup()
    tip = checkout(Path(arguments[0]))
    with tempfile.TemporaryDirectory(prefix="playbook-check-") as directory:
        scratch = Path(directory)
        refused = check(tip, role_spec=ansible.role_spec(scratch), modules=ansible.modules(scratch))
    for reason in refused:
        print(reason, file=sys.stderr)
    sidecars = sum(1 for path, _ in tip.files if path.endswith(catalog.SIDECAR))
    if refused:
        print(f"HQ would refuse what is above, of the {sidecars} playbooks at {tip.project_dir}", file=sys.stderr)
        return 1
    print(f"HQ would adopt each of the {sidecars} playbooks at {tip.project_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
