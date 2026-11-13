"""What the worker's repository offers at the tip of its default branch: each
procedure it declares, read without running anything of it.

A procedure is a playbook with a sidecar beside it, ``<name>.hq.yml`` next to
``<name>.yml``, anywhere in the project. The sidecar says what Ansible has no
word for (its id, what it acts on, its effect); everything else is Ansible's
own and read from where Ansible keeps it: the title from the first play's
name, the preconditions and verification from the names of the
``ansible.builtin.assert`` tasks in its plays' ``pre_tasks`` and
``post_tasks``, and its inputs from the argument spec of each role its plays
list, as ``ansible-doc`` reads it (``ansible.role_spec``).

The worker reads; HQ judges. A sidecar is reported as parsed, and HQ holds it
to the declaration's model, so the model has one home and a proposal says
every way a sidecar falls short. What the worker cannot read (a file that is
not YAML, a role ansible-doc cannot find) it reports as a problem of that
procedure, and the rest of the catalog still arrives.

Alongside, every file of the project with its git object id, which names its
content, and each path Ansible would read beyond a playbook and its roles
(``unpinned``): a configuration file the worker never reads, and directories
Ansible loads code or variables from wherever they sit. Whether a commit
carrying one may be adopted is HQ's decision; the worker only says it is
there. And the commit's verification as GitHub reports it, so HQ can require
a signed commit of a repository whose plan offers no rule that does.
"""

import http
import http.client
import json
import re
from collections.abc import Callable, Iterable, Mapping
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

lazy import yaml

from . import procedures
from .ansible import RoleSpec
from .claim import Refused

SIDECAR = ".hq.yml"
PLAYBOOK = ".yml"
# The most of each a catalog carries; past them the scan is refused, since a
# catalog with some procedures missing would read as their removal.
MOST_PROCEDURES = 200
MOST_FILES = 10_000
# The most bytes of a sidecar or a playbook read.
SIDECAR_BYTES = 64 << 10
PLAYBOOK_BYTES = 1 << 20
# Bounds on what one procedure reports, as the bridge contract's
# ``ScannedProcedure`` holds them.
TITLE_LIMIT = 200
STATEMENT_LIMIT = 500
MOST_STATEMENTS = 50
MOST_ROLES = 20
PROBLEM_LIMIT = 500
MOST_PROBLEMS = 20
# The longest path a file reported may have: Linux's PATH_MAX, which a path
# checked out under the scratch directory stays within.
PATH_LIMIT = 4096

# The directories Ansible adds to its plugin search when one sits beside a
# playbook or inside a role, by ansible-core 2.22's plugin loaders' subdir
# names (``plugins.loader.get_all_plugin_loaders``; the smoke holds this to
# them), with ``collections``, which it searches beside a playbook, and
# ``plugins``, a collection's own layout.
PLUGIN_DIRECTORIES = frozenset(
    {
        "action_plugins",
        "become_plugins",
        "cache_plugins",
        "callback_plugins",
        "cliconf_plugins",
        "collections",
        "connection_plugins",
        "doc_fragments",
        "filter_plugins",
        "httpapi_plugins",
        "inventory_plugins",
        "library",
        "lookup_plugins",
        "module_utils",
        "netconf_plugins",
        "plugins",
        "shell_plugins",
        "strategy_plugins",
        "terminal_plugins",
        "test_plugins",
        "vars_plugins",
    }
)
# Where Ansible reads variables for every play beside a playbook or an inventory.
VARIABLE_DIRECTORIES = frozenset({"group_vars", "host_vars"})
CONFIG = "ansible.cfg"
# The action an assertion is, in each spelling Ansible resolves to it.
ASSERT = frozenset({"assert", "ansible.builtin.assert", "ansible.legacy.assert"})

GITHUB_API = "api.github.com"
# How long GitHub may take to answer, and the most bytes of its answer read.
API_TIMEOUT_SECONDS = 30
API_BYTES = 4 << 20
# A verification reason as GitHub spells one (``valid``, ``unsigned``, ...).
_REASON = re.compile(r"[a-z_]{1,40}")
# A GitHub login: what ``committer.login`` holds.
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

# Whether GitHub verified the commit ``revision``'s signature, as
# ``{"verified", "reason", "committer"}``. It may refuse.
Verify = Callable[[str], Mapping[str, Any]]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# A role as a play may name one for ansible-doc to find: a name, or a
# collection's dotted one, never a path or anything read as an option.
_ROLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}")


def _untagged(loader: Any, _suffix: str, node: Any) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


@cache
def _loader() -> type:
    """YAML as Ansible's files hold it: a value Ansible tags (``!vault``,
    ``!unsafe``) is read as the plain value under the tag, never decrypted or
    run. Built on first use, so a run never loads YAML."""

    loader: type[yaml.SafeLoader] = type("AnsibleYAML", (yaml.SafeLoader,), {})
    loader.add_multi_constructor("!", _untagged)
    return loader


def _yaml(project_dir: Path, path: str, limit: int) -> Any:
    """The YAML document at ``path`` in ``project_dir``, read up to ``limit``
    bytes once every link is followed and stays inside it."""

    with procedures.within(project_dir, path).open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"it is over {limit} bytes")
    return yaml.load(raw, Loader=_loader())  # noqa: S506 a SafeLoader, which constructs no object


def unpinned(files: Iterable[str]) -> list[dict[str, str]]:
    """Each path of ``files`` Ansible would read beyond a playbook and its
    roles' tasks, once each: its directory (or file) and what it holds."""

    found: dict[str, str] = {}
    for path in files:
        parts = PurePosixPath(path).parts
        if parts[-1] == CONFIG:
            found.setdefault(path, "config")
        for depth, part in enumerate(parts[:-1]):
            kind = "plugins" if part in PLUGIN_DIRECTORIES else "variables" if part in VARIABLE_DIRECTORIES else ""
            if kind:
                found.setdefault("/".join(parts[: depth + 1]), kind)
                break
    return [{"path": path, "kind": kind} for path, kind in sorted(found.items())]


def _tasks(play: Mapping[str, Any], section: str) -> list[str]:
    """The names of the assertions in one section of a play."""

    tasks = play.get(section)
    return [
        _clip(task["name"], STATEMENT_LIMIT)
        for task in (tasks if isinstance(tasks, list) else ())
        if isinstance(task, Mapping) and ASSERT & set(task) and isinstance(task.get("name"), str)
    ]


def _roles(play: Mapping[str, Any]) -> list[str]:
    """The roles a play lists, each by the name Ansible finds it by."""

    roles = play.get("roles")
    named: list[str] = []
    for role in roles if isinstance(roles, list) else ():
        name = role.get("role", role.get("name")) if isinstance(role, Mapping) else role
        if isinstance(name, str) and name and name not in named:
            named.append(name)
    return named


class _Procedure:
    """One procedure as it is read, gathering what could not be."""

    def __init__(self, sidecar: str, playbook: str) -> None:
        self.found: dict[str, Any] = {
            "sidecar": sidecar,
            "playbook": playbook,
            "declaration": None,
            "title": "",
            "roles": [],
            "preconditions": [],
            "verification": [],
            "problems": [],
        }

    def problem(self, said: str) -> None:
        problems = self.found["problems"]
        if len(problems) < MOST_PROBLEMS:
            problems.append(_clip(said, PROBLEM_LIMIT))

    def read_sidecar(self, project_dir: Path) -> None:
        try:
            declaration = _yaml(project_dir, self.found["sidecar"], SIDECAR_BYTES)
        except Refused as refused:
            self.problem(refused.message)
            return
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.problem(f"Its sidecar could not be read: {exc}")
            return
        if not isinstance(declaration, Mapping):
            self.problem("Its sidecar is not a mapping.")
            return
        # As JSON reads it, so what HQ holds to the model is what was written.
        self.found["declaration"] = json.loads(json.dumps(declaration, default=str))

    def read_playbook(self, project_dir: Path) -> list[str]:
        """Its plays' title and assertions, and the roles to read the specs of."""

        try:
            plays = _yaml(project_dir, self.found["playbook"], PLAYBOOK_BYTES)
        except Refused as refused:
            self.problem(refused.message)
            return []
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.problem(f"Its playbook could not be read: {exc}")
            return []
        if not isinstance(plays, list):
            self.problem("Its playbook is not a list of plays.")
            return []
        plays = [play for play in plays if isinstance(play, Mapping) and "hosts" in play]
        title = next((play["name"] for play in plays if isinstance(play.get("name"), str)), "")
        self.found["title"] = _clip(title, TITLE_LIMIT)
        if not title:
            self.problem("Its first play has no name, which is its title.")
        for section, key in (("pre_tasks", "preconditions"), ("post_tasks", "verification")):
            self.found[key] = [name for play in plays for name in _tasks(play, section)][:MOST_STATEMENTS]
        roles = list(dict.fromkeys(role for play in plays for role in _roles(play)))
        for role in [role for role in roles if not _ROLE.fullmatch(role)]:
            self.problem(f"Its plays name the role {role!r} by something other than a role's name.")
            roles.remove(role)
        if len(roles) > MOST_ROLES:
            self.problem(f"Its plays list {len(roles)} roles; a procedure takes the inputs of {MOST_ROLES} at most.")
        return roles[:MOST_ROLES]

    def read_roles(self, roles: list[str], project_dir: Path, role_spec: RoleSpec) -> None:
        playbook_dir = (project_dir / self.found["playbook"]).parent
        for role in roles:
            try:
                spec = role_spec(role, playbook_dir, project_dir)
            except Refused as refused:
                self.problem(refused.message)
                spec = None
            self.found["roles"].append({"role": role, "argument_spec": spec})


def procedure(project_dir: Path, sidecar: str, files: frozenset[str], role_spec: RoleSpec) -> dict[str, Any]:
    """The procedure the sidecar at ``sidecar`` declares, as the worker reads it."""

    playbook = sidecar.removesuffix(SIDECAR) + PLAYBOOK
    found = _Procedure(sidecar, playbook if playbook in files else "")
    found.read_sidecar(project_dir)
    if playbook not in files:
        found.problem(f"No playbook {playbook} sits beside it.")
        return found.found
    roles = found.read_playbook(project_dir)
    found.read_roles(roles, project_dir, role_spec)
    return found.found


def scan(
    project: procedures.Project, *, scratch: Path, token: str, role_spec: RoleSpec, verify: Verify
) -> dict[str, Any]:
    """The catalog of ``project`` at the tip of its default branch, as the
    bridge contract's ``SourceCatalog`` holds it."""

    tip = procedures.tip(project, scratch=scratch, token=token)
    if len(tip.files) > MOST_FILES:
        raise Refused("TooLarge", f"The project holds {len(tip.files)} files; a scan reads {MOST_FILES} at most.")
    paths = tuple(path for path, _ in tip.files)
    sidecars = [path for path in paths if path.endswith(SIDECAR) and procedures.safe_path(path, suffix=SIDECAR)]
    if len(sidecars) > MOST_PROCEDURES:
        raise Refused("TooLarge", f"The project declares {len(sidecars)} procedures; a scan reads {MOST_PROCEDURES}.")
    files = frozenset(paths)
    return {
        "branch": tip.branch,
        "revision": tip.revision,
        "verification": dict(verify(tip.revision)),
        "procedures": [procedure(tip.project_dir, sidecar, files, role_spec) for sidecar in sidecars],
        "files": [{"path": path, "object": name} for path, name in tip.files],
        "unpinned": unpinned(paths),
    }


def verification(payload: Any) -> dict[str, Any]:
    """What GitHub's answer for one commit (``GET /repos/{owner}/{repo}/commits/{ref}``)
    says of its signature, and the GitHub account that committed it."""

    commit = payload.get("commit") if isinstance(payload, Mapping) else None
    verified = commit.get("verification") if isinstance(commit, Mapping) else None
    if not isinstance(verified, Mapping) or not isinstance(verified.get("verified"), bool):
        raise Refused("VerificationUnread", "GitHub's answer for the commit says nothing of its signature.")
    reason = verified.get("reason")
    committer = payload.get("committer")
    login = committer.get("login") if isinstance(committer, Mapping) else None
    return {
        "verified": verified["verified"],
        "reason": reason if isinstance(reason, str) and _REASON.fullmatch(reason) else "unknown",
        "committer": login if isinstance(login, str) and _LOGIN.fullmatch(login) else "",
    }


def github_verify(project: procedures.Project, token: str, *, host: str = GITHUB_API) -> Verify:
    """A reader of each commit's verification from GitHub's REST API, for a
    repository reached on GitHub; for any other, every commit is unverified,
    since only GitHub says.

    Over ``http.client``, which follows no redirect, so the token goes to
    GitHub's API alone.
    """

    def verify(revision: str) -> Mapping[str, Any]:
        if project.origin != procedures.GITHUB:
            return {"verified": False, "reason": "unavailable", "committer": ""}
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hq-runner",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        }
        connection = http.client.HTTPSConnection(host, timeout=API_TIMEOUT_SECONDS)
        try:
            connection.request("GET", f"/repos/{project.repository}/commits/{revision}", headers=headers)
            answer = connection.getresponse()
            raw = answer.read(API_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise Refused("VerificationUnread", "GitHub did not say whether the commit is signed (no answer).") from exc
        finally:
            connection.close()
        if answer.status != http.HTTPStatus.OK:
            raise Refused("VerificationUnread", f"GitHub did not say whether the commit is signed ({answer.status}).")
        if len(raw) > API_BYTES:
            raise Refused("VerificationUnread", "GitHub's answer for the commit is too large.")
        try:
            return verification(json.loads(raw))
        except ValueError as exc:
            raise Refused("VerificationUnread", "GitHub's answer for the commit is not JSON.") from exc

    return verify
