"""The reviewed procedure a claim names, fetched at its pinned commit, and the
tip of the repository's default branch a scan reads.

A procedure's playbook is a path inside one repository at one commit: the
commit HQ's manifest pins and its reviewers approved, in the repository this
worker's own configuration names (never the claim), whose root is an Ansible
project. Each run fetches that commit alone into a fresh directory, as Flux's
source-controller and Argo CD's repo-server fetch a pinned revision, and runs
only once the checkout's ``HEAD`` is that commit: a commit id names its whole
tree, so no other tree can answer it, and an id that names something else (a
tag object, a tree) is refused. A scan asks the repository which commit its
default branch points at (``tip``), then fetches that commit the same way.

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
    """Where this worker's procedures come from: a repository, whose root is
    the Ansible project, and where on this machine it is reached."""

    repository: str
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


def within(root: Path, path: str, *, suffix: str = "") -> Path:
    """``path`` inside ``root`` once every link is followed, or Refused."""

    if not safe_path(path, suffix=suffix) and path != ".":
        raise Refused("Unrunnable", f"The procedure's path {path!r} is not a plain relative path.")
    found = (root / PurePosixPath(path)).resolve()
    if not found.is_relative_to(root.resolve()):
        raise Refused("Unrunnable", f"The procedure's path {path!r} leaves its checkout.")
    return found


def _checked(project: Project) -> str:
    """The transport ``project`` is reached by, once it is one a fetch may use."""

    if not _REPOSITORY.fullmatch(project.repository):
        raise Refused("Unrunnable", "This worker's repository is not an owner/name.")
    transport = _TRANSPORTS.get(project.origin.partition("://")[0])
    if transport is None:
        raise Refused("Unrunnable", "This worker's repository is reached by no transport it allows.")
    return transport


def _empty(project: Project, scratch: Path, token: str) -> tuple[Path, dict[str, str]]:
    """A new, empty repository in a private directory under ``scratch``, and
    the environment git runs in there."""

    transport = _checked(project)
    into = Path(tempfile.mkdtemp(prefix="project-", dir=scratch))
    environment = _git_environment(into, transport, token)
    checkout = into / "checkout"
    checkout.mkdir()
    _git(checkout, environment, "init", "--quiet")
    return checkout, environment


def _at(project: Project, revision: str, checkout: Path, environment: Mapping[str, str]) -> Path:
    """``checkout`` once it holds ``revision`` alone."""

    _git(checkout, environment, "fetch", "--quiet", "--no-tags", "--depth", "1", project.url(), revision)
    _git(checkout, environment, "checkout", "--quiet", "--detach", revision)
    head = _git(checkout, environment, "rev-parse", "--verify", "HEAD")
    if head != revision:
        raise Refused("RevisionMismatch", f"The commit {revision[:12]} checks out as {head[:12]}, another tree.")
    # Resolved, as every path read inside it is (``within``).
    return checkout.resolve()


def fetch(project: Project, revision: str, playbook: str, *, scratch: Path, token: str = "") -> Procedure:
    """The procedure at ``playbook`` in ``project`` at ``revision``, checked out
    into a new private directory under ``scratch``."""

    if not _REVISION.fullmatch(revision):
        raise Refused("Unrunnable", "The run names no reviewed commit.")
    if not safe_path(playbook, suffix=".yml"):
        raise Refused("Unrunnable", f"The procedure's playbook {playbook!r} is not a plain relative .yml path.")
    checkout, environment = _empty(project, scratch, token)
    directory = _at(project, revision, checkout, environment)
    found = within(directory, playbook, suffix=".yml")
    if not found.is_file() or found.suffix != ".yml":
        raise Refused("NotInstalled", f"The commit {revision[:12]} has no playbook {playbook}.")
    return Procedure(revision, directory, str(found.relative_to(directory)))


@dataclass(frozen=True, slots=True)
class Tip:
    """The commit a repository's default branch pointed at when it was asked,
    checked out, with every file it holds."""

    branch: str
    revision: str
    project_dir: Path
    # Each file's path and its git object id,
    # which names its content.
    files: tuple[tuple[str, str], ...]


# A branch's name as git allows one, bounded: what ``ls-remote --symref``
# says ``HEAD`` is, ``refs/heads/<name>``.
_BRANCH = re.compile(r"ref: refs/heads/([A-Za-z0-9._/-]{1,200})\tHEAD")
_OBJECT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _files(checkout: Path, environment: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Every file of the checkout's ``HEAD``, by its path."""

    found: list[tuple[str, str]] = []
    for entry in _git(checkout, environment, "ls-tree", "-r", "-z", "--full-tree", "HEAD").split("\0"):
        described, _, path = entry.partition("\t")
        _mode, kind, name = [*described.split(" "), "", "", ""][:3]
        # A submodule's entry is a commit, which a fetch never brings.
        if kind == "blob" and _OBJECT.fullmatch(name):
            found.append((path, name))
    return tuple(sorted(found))


def tip(project: Project, *, scratch: Path, token: str = "") -> Tip:
    """The tip of ``project``'s default branch, checked out into a new private
    directory under ``scratch``."""

    checkout, environment = _empty(project, scratch, token)
    said = _git(checkout, environment, "ls-remote", "--symref", project.url(), "HEAD")
    branch = _BRANCH.search(said)
    pointed = [line.partition("\t")[0] for line in said.splitlines() if line.endswith("\tHEAD")]
    revision = next((name for name in pointed if _REVISION.fullmatch(name)), "")
    if branch is None or not revision:
        raise Refused("FetchFailed", "The repository names no default branch.")
    directory = _at(project, revision, checkout, environment)
    return Tip(branch[1], revision, directory, _files(checkout, environment))


def resolver(project: Project, scratch: Path, *, token: str = "") -> Installed:
    """A resolver that fetches each procedure into its own directory under ``scratch``."""

    # Every procedure of a project is found by its playbook's path alone.
    def installed(_procedure_id: str, revision: str, playbook: str) -> Procedure:
        return fetch(project, revision, playbook, scratch=scratch, token=token)

    return installed
