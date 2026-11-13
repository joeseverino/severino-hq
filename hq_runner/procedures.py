"""The reviewed procedure a claim names, fetched at its pinned commit.

A procedure's playbook is a path inside one repository at one commit: the
commit HQ's manifest pins and its reviewers approved, the repository and the
project directory within it this worker's own configuration names (never the
claim). Each run fetches that commit alone into a fresh directory, as Flux's
source-controller and Argo CD's repo-server fetch a pinned revision, and runs
only once the checkout's ``HEAD`` is that commit: a commit id names its whole
tree, so no other tree can answer it, and an id that names something else (a
tag object, a tree) is refused.

Git reads no configuration but what is given here (no system or global file,
no hooks, no submodules, one transport) and checks every object it receives.
The token, when there is one, reaches git through its environment, never its
command line, where any process on the machine could read it.
"""

import base64
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .claim import Installed, Procedure, Refused

GITHUB = "https://github.com"
# owner/name, as GitHub spells a repository.
_REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
_REVISION = re.compile(r"[0-9a-f]{40}")
# A path part that is a name: no empty part, no `.` or `..`, nothing hidden
# and nothing ansible-playbook would read as an option.
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
# The transports a fetch may use: https for every real repository, and a local
# repository for a test's.
_TRANSPORTS = {"https": "https", "file": "file"}
# How long one git command may take.
GIT_TIMEOUT_SECONDS = 300


def safe_path(path: str, *, suffix: str = "") -> bool:
    """Whether ``path`` is relative and made only of plain names, ending ``suffix``."""

    parts = path.split("/")
    return bool(path) and path.endswith(suffix) and all(_PART.fullmatch(part) for part in parts)


@dataclass(frozen=True, slots=True)
class Project:
    """Where this worker's procedures come from: a repository, the directory
    inside it the playbooks run from, and where on this machine it is reached."""

    repository: str
    directory: str = "."
    origin: str = GITHUB

    def url(self) -> str:
        return f"{self.origin}/{self.repository}.git"


def _git_environment(scratch: Path, transport: str, token: str) -> dict[str, str]:
    settings = {
        "core.hooksPath": "/dev/null",
        "transfer.fsckObjects": "true",
        "protocol.allow": "never",
        f"protocol.{transport}.allow": "always",
        "submodule.recurse": "false",
        "fetch.recurseSubmodules": "false",
        "advice.detachedHead": "false",
        "init.defaultBranch": "main",
    }
    if token:
        # GitHub reads an installation token as the password of the user
        # x-access-token, as actions/checkout sends it.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        settings["http.extraHeader"] = f"Authorization: Basic {basic}"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(scratch),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_CONFIG_COUNT": str(len(settings)),
    }
    for index, (key, value) in enumerate(settings.items()):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _git(checkout: Path, environment: Mapping[str, str], *arguments: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 fixed arguments, the commit id and playbook checked first
            ["git", "-C", str(checkout), *arguments],  # noqa: S607 the image's git, from the PATH its launcher sets
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused("FetchFailed", f"git {arguments[0]} did not finish.") from exc
    if done.returncode != 0:
        raise Refused("FetchFailed", f"git {arguments[0]} failed: {' '.join(done.stderr.split())[:300]}")
    return done.stdout.strip()


def _within(root: Path, path: str, *, suffix: str = "") -> Path:
    """``path`` inside ``root`` once every link is followed, or Refused."""

    if not safe_path(path, suffix=suffix) and path != ".":
        raise Refused("Unrunnable", f"The procedure's path {path!r} is not a plain relative path.")
    found = (root / PurePosixPath(path)).resolve()
    if not found.is_relative_to(root.resolve()):
        raise Refused("Unrunnable", f"The procedure's path {path!r} leaves its checkout.")
    return found


def fetch(project: Project, revision: str, playbook: str, *, scratch: Path, token: str = "") -> Procedure:
    """The procedure at ``playbook`` in ``project`` at ``revision``, checked out
    into a new private directory under ``scratch``."""

    if not _REVISION.fullmatch(revision):
        raise Refused("Unrunnable", "The run names no reviewed commit.")
    if not safe_path(playbook, suffix=".yml"):
        raise Refused("Unrunnable", f"The procedure's playbook {playbook!r} is not a plain relative .yml path.")
    if not _REPOSITORY.fullmatch(project.repository):
        raise Refused("Unrunnable", "This worker's repository is not an owner/name.")
    transport = _TRANSPORTS.get(project.origin.partition("://")[0])
    if transport is None:
        raise Refused("Unrunnable", "This worker's repository is reached by no transport it allows.")
    into = Path(tempfile.mkdtemp(prefix="project-", dir=scratch))
    environment = _git_environment(into, transport, token)
    checkout = into / "checkout"
    checkout.mkdir()
    _git(checkout, environment, "init", "--quiet")
    _git(checkout, environment, "fetch", "--quiet", "--no-tags", "--depth", "1", project.url(), revision)
    _git(checkout, environment, "checkout", "--quiet", "--detach", revision)
    head = _git(checkout, environment, "rev-parse", "--verify", "HEAD")
    if head != revision:
        raise Refused("RevisionMismatch", f"The commit {revision[:12]} checks out as {head[:12]}, another tree.")
    directory = _within(checkout, project.directory)
    if not directory.is_dir():
        raise Refused("NotInstalled", f"The commit {revision[:12]} has no directory {project.directory}.")
    found = _within(directory, playbook, suffix=".yml")
    if not found.is_file() or found.suffix != ".yml":
        raise Refused("NotInstalled", f"The commit {revision[:12]} has no playbook {playbook}.")
    return Procedure(revision, directory, str(found.relative_to(directory)))


def resolver(project: Project, scratch: Path, *, token: str = "") -> Installed:
    """A resolver that fetches each procedure into its own directory under ``scratch``."""

    # Every procedure of a project is found by its playbook's path alone.
    def installed(_procedure_id: str, revision: str, playbook: str) -> Procedure:
        return fetch(project, revision, playbook, scratch=scratch, token=token)

    return installed
