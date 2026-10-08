"""The standard your repositories are held to, and whether each meets it.

Two standards from one list. Every repository is held to what protects code
wherever it lives (who has access, what keys can do, how Actions may run); a
public one is held to more, because GitHub gives public repositories more:
scanning, push protection, rules on the default branch. What a plan does not
offer is "not available", never a failure.

Checked against the GitHub App's own reading (``github_estate``), so nothing
here asks GitHub anything, and a check HQ cannot read says so.
"""

import json
import shlex
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from hq.platform.application.routes import reverse

from .derivations import passed
from .github_estate import Repository, attention as repository_attention, repositories
from .item_help import cannot_help, do_step, run_step, steps
from .standards import UNMET, Check as _Check, Posture, measure
from .timestamps import moment
from .ui import Insight, counted

BOTH = "both"
PUBLIC = "public"
# A deploy key nobody has used in this long is either dead weight or moved.
KEY_IDLE_DAYS = 90


def _public(repo: Repository) -> bool:
    return not repo.private


def Check(id: str, label: str, scope: str, test, why: str, fix: str, *, serious: bool = False) -> _Check:
    """A repository check: every repository's, or a public one's besides."""

    return _Check(id, label, test, why, fix, serious=serious, scope=scope, applies=_public if scope == PUBLIC else None)


def _access(repo: Repository) -> dict[str, Any] | None:
    return repo.record.get("access")


def _only_you(repo: Repository) -> bool | None:
    access = _access(repo)
    if access is None:
        return None
    owner = repo.name.split("/", 1)[0].lower()
    return all(item["login"].lower() == owner for item in access["collaborators"])


def _keys_read_only(repo: Repository) -> bool | None:
    access = _access(repo)
    return None if access is None else all(key["read_only"] for key in access["deploy_keys"])


def _idle(key: dict[str, Any]) -> bool:
    seen = moment(key.get("last_used") or key.get("created_at") or "")
    return seen is not None and passed(seen + timedelta(days=KEY_IDLE_DAYS))


def _keys_in_use(repo: Repository) -> bool | None:
    access = _access(repo)
    return None if access is None else not any(_idle(key) for key in access["deploy_keys"])


def _setting(name: str, expected: Any) -> Callable[[Repository], bool | None]:
    def test(repo: Repository) -> bool | None:
        access = _access(repo)
        return None if access is None else access.get(name) == expected

    return test


def _no_variables(repo: Repository) -> bool | None:
    variables = repo.record.get("variables")
    return None if variables is None else not variables


def _rule(name: str) -> Callable[[Repository], bool | None]:
    def test(repo: Repository) -> bool | None:
        rules = repo.record.get("rules")
        return None if rules is None else bool(rules.get(name))

    return test


def _security(name: str) -> Callable[[Repository], bool | None]:
    def test(repo: Repository) -> bool | None:
        security = (_access(repo) or {}).get("security")
        return None if not security else security.get(name) == "enabled"

    return test


def _code_scanning(repo: Repository) -> bool | None:
    return "code_scanning" in (repo.record.get("alerts") or {}) or None


STANDARD: tuple[Check, ...] = (
    Check(
        "only-you",
        "Only you have access",
        BOTH,
        _only_you,
        "Anyone else with access can read private code or push to it.",
        "Remove the collaborator in the repository's access settings.",
        serious=True,
    ),
    Check(
        "keys-read-only",
        "Deploy keys can only read",
        BOTH,
        _keys_read_only,
        "A deploy key that can write can push to the repository without you.",
        "Replace the key with a read-only one.",
        serious=True,
    ),
    Check(
        "keys-in-use",
        "Every deploy key is in use",
        BOTH,
        _keys_in_use,
        f"A key unused for {KEY_IDLE_DAYS} days is access nothing needs.",
        "Delete the key, or find what stopped using it.",
    ),
    Check(
        "token-read-only",
        "The workflow token is read-only",
        BOTH,
        _setting("token", "read"),
        "A writable default token lets any workflow change the repository.",
        "Set workflow permissions to read in the Actions settings.",
    ),
    Check(
        "token-no-approvals",
        "Workflows cannot approve pull requests",
        BOTH,
        _setting("token_approves_reviews", False),
        "A workflow that approves its own pull request skips review.",
        "Turn off approving pull requests in the Actions settings.",
    ),
    Check(
        "actions-pinned",
        "Actions must be pinned to a commit",
        BOTH,
        _setting("pinning_required", True),
        "A tag can be moved to different code; a commit cannot.",
        "Pin every action its workflows use to a commit, then require pinning in the Actions settings.",
    ),
    Check(
        "security-fixes",
        "Dependabot opens security fixes",
        BOTH,
        _setting("security_fixes", True),
        "A known-vulnerable dependency waits for you to notice it.",
        "Turn on Dependabot security updates.",
    ),
    Check(
        "no-variables",
        "No Actions variables",
        BOTH,
        _no_variables,
        "A variable is state outside the repository that changes what a build does.",
        "Move it into the workflow or a secret, or delete it.",
    ),
    Check(
        "pull-request-required",
        "Changes arrive by pull request",
        PUBLIC,
        _rule("pull_request"),
        "A direct push to the default branch skips every check.",
        "Require a pull request in the default branch's ruleset.",
    ),
    Check(
        "force-push-blocked",
        "The default branch cannot be rewritten",
        PUBLIC,
        _rule("blocks_force_push"),
        "A force push rewrites history others have already built on.",
        "Block force pushes in the default branch's ruleset.",
    ),
    Check(
        "deletion-blocked",
        "The default branch cannot be deleted",
        PUBLIC,
        _rule("blocks_deletion"),
        "Deleting the default branch takes every deploy with it.",
        "Block deletion in the default branch's ruleset.",
    ),
    Check(
        "secret-scanning",
        "Leaked credentials are scanned for",
        PUBLIC,
        _security("secret_scanning"),
        "A credential committed to a public repository is found by others first.",
        "Turn on secret scanning.",
    ),
    Check(
        "push-protection",
        "Credentials are stopped before they are pushed",
        PUBLIC,
        _security("secret_scanning_push_protection"),
        "Scanning finds a leak after it is public; push protection stops it.",
        "Turn on push protection.",
    ),
    Check(
        "code-scanning",
        "Code is scanned",
        PUBLIC,
        _code_scanning,
        "Code scanning finds vulnerable patterns before a reviewer does.",
        "Turn on code scanning.",
    ),
)


# The secrets the host's own wiring sets (scripts/wire-github-app.py). A
# variable by one of these names is one of them kept in the wrong place.
WIRED_SECRETS = frozenset({"HQ_APP_KEY", "HQ_APP_CLIENT_ID"})

Runs = tuple[tuple[str, str], ...]


def _api(repo: Repository, method: str, path: str, *fields: str) -> str:
    return " ".join((f"gh api -X {method} repos/{repo.name}{path}", *fields)).rstrip()


def _others(repo: Repository) -> Runs:
    owner = repo.name.split("/", 1)[0].lower()
    return tuple(
        (f"{repo.short}: remove {item['login']}", _api(repo, "DELETE", f"/collaborators/{item['login']}"))
        for item in (_access(repo) or {}).get("collaborators", ())
        if item["login"].lower() != owner
    )


def _delete_key(repo: Repository, title: str) -> str:
    """Deletes a deploy key by its title: the reading names a key, not its ID."""

    found = shlex.quote(f".[] | select(.title == {json.dumps(title)}) | .id")
    return _api(repo, "DELETE", f"/keys/$(gh api repos/{repo.name}/keys --jq {found})")


def _writable_keys(repo: Repository) -> Runs:
    return tuple(
        (
            f"{repo.short}: delete the writable key {key['title']}, then add its public key back "
            f"with gh repo deploy-key add -R {repo.name}, which is read-only unless told otherwise",
            _delete_key(repo, key["title"]),
        )
        for key in (_access(repo) or {}).get("deploy_keys", ())
        if not key["read_only"]
    )


def _idle_keys(repo: Repository) -> Runs:
    return tuple(
        (f"{repo.short}: delete the unused key {key['title']}", _delete_key(repo, key["title"]))
        for key in (_access(repo) or {}).get("deploy_keys", ())
        if _idle(key)
    )


def _variables(repo: Repository) -> Runs:
    """Each variable's commands. One the host's wiring declares as a secret moves
    into a secret first, and goes only once the workflows read the secret."""

    runs: list[tuple[str, str]] = []
    for name in repo.record.get("variables") or ():
        delete = f"gh variable delete {name} -R {repo.name}"
        if name not in WIRED_SECRETS:
            runs.append((f"{repo.short}: delete {name}", delete))
            continue
        runs += [
            (
                f"{repo.short}: 1. copy {name} into a secret of the same name",
                f'gh secret set {name} -R {repo.name} --body "$(gh variable get {name} -R {repo.name})"',
            ),
            (f"{repo.short}: 2. only after its workflows read secrets.{name}, delete the variable", delete),
        ]
    return tuple(runs)


def _setting(*fields: str, method: str = "PUT", path: str) -> Callable[[Repository], Runs]:
    return lambda repo: ((repo.short, _api(repo, method, path, *fields)),)


def _ruleset(name: str, rule: dict[str, Any]) -> Callable[[Repository], Runs]:
    """A ruleset on the default branch holding one rule."""

    body = json.dumps(
        {
            "name": name,
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [rule],
        },
        separators=(",", ":"),
    )
    return lambda repo: ((repo.short, f"{_api(repo, 'POST', '/rulesets', '--input -')} <<< {shlex.quote(body)}"),)


def _security_fixes(repo: Repository) -> Runs:
    return (
        (
            repo.short,
            f"{_api(repo, 'PUT', '/vulnerability-alerts')} && {_api(repo, 'PUT', '/automated-security-fixes')}",
        ),
    )


def _literal(text: str) -> str:
    """``text`` as a literal in a Perl pattern or replacement.

    Every character but a word character is escaped, which also stops ``@``
    and ``$`` interpolating.
    """

    return "".join(char if char.isalnum() or char == "_" else f"\\{char}" for char in text)


def _pin_edit(pin: dict[str, Any]) -> str:
    """One Perl substitution: the ``uses:`` line, pinned, with the tag kept as a comment.

    Only a line whose key is ``uses:`` (after indentation and an optional
    ``- ``) matches, so a commented-out line is never touched. Trailing spaces
    and a comment already there are replaced; a CRLF ending is kept.
    """

    pinned = _literal(f"uses: {pin['action']}@{pin['sha']} # {pin['ref']}")
    return (
        rf"""s/^([ \t]*(?:-[ \t]+)?)uses:[ \t]*(["']?){_literal(pin["uses"])}\2"""
        rf"""(?:[ \t]+#[^\r\n]*)?[ \t]*(\r?)$/${{1}}{pinned}${{3}}/;"""
    )


def _pinning(repo: Repository) -> Runs:
    """Each file's edit, as one command to paste in a checkout, then the setting.

    The commit each tag names now, as the App read it. Perl rather than sed,
    because ``sed -i`` takes its arguments differently on macOS and on Linux.
    The setting comes last and only when every line could be resolved and no
    workflow is called from another repository: requiring pinning before the
    workflows are pinned stops every run, and it applies as much to what a
    called workflow uses, which HQ did not read.
    """

    if pins_unread(repo):
        # Nothing to pin is not the same as not knowing what to pin.
        return ()
    pins = repo.record.get("pins") or []
    runs: list[tuple[str, str]] = []
    by_path: dict[str, list[dict[str, Any]]] = {}
    for pin in pins:
        by_path.setdefault(str(pin.get("path", "")), []).append(pin)
    for path, found in sorted(by_path.items()):
        edits = dict.fromkeys(_pin_edit(pin) for pin in found if pin.get("sha"))
        if edits:
            script = shlex.quote(" ".join(edits))
            runs.append((f"{repo.short}: pin {path}", f"perl -pi -e {script} {shlex.quote(path)}"))
    if not pins_unresolved(repo) and not calls_workflows(repo):
        runs.append(
            (
                f"{repo.short}: then require pinning",
                _api(repo, "PUT", "/actions/permissions", "-F enabled=true", "-F sha_pinning_required=true"),
            )
        )
    return tuple(runs)


def pins_unread(repo: Repository) -> bool:
    return repo.record.get("pins") is None


def pins_unresolved(repo: Repository) -> tuple[str, ...]:
    return tuple(sorted({str(pin.get("uses", "")) for pin in repo.record.get("pins") or () if not pin.get("sha")}))


def calls_workflows(repo: Repository) -> tuple[str, ...]:
    """The workflows from other repositories this one calls, as its ``uses:`` lines name them."""

    return tuple(repo.record.get("called_workflows") or ())


_ANALYSIS = "security_and_analysis[{}][status]=enabled"
# The exact commands that meet a check, per repository that misses it.
COMMANDS: dict[str, Callable[[Repository], Runs]] = {
    "only-you": _others,
    "keys-read-only": _writable_keys,
    "keys-in-use": _idle_keys,
    "token-read-only": _setting("-f default_workflow_permissions=read", path="/actions/permissions/workflow"),
    "token-no-approvals": _setting("-F can_approve_pull_request_reviews=false", path="/actions/permissions/workflow"),
    "security-fixes": _security_fixes,
    "no-variables": _variables,
    "pull-request-required": _ruleset(
        "Require a pull request",
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        },
    ),
    "force-push-blocked": _ruleset("Block force pushes", {"type": "non_fast_forward"}),
    "deletion-blocked": _ruleset("Block deletion", {"type": "deletion"}),
    "secret-scanning": _setting(f"-f '{_ANALYSIS.format('secret_scanning')}'", method="PATCH", path=""),
    "push-protection": _setting(f"-f '{_ANALYSIS.format('secret_scanning_push_protection')}'", method="PATCH", path=""),
    "code-scanning": _setting("-f state=configured", method="PATCH", path="/code-scanning/default-setup"),
    "actions-pinned": _pinning,
}
# Why HQ offers no command for a check it cannot safely derive one for.
REASONS: dict[str, str] = {}
# Said beside a pinning plan for a repository whose workflows were not read.
PINS_UNREAD = (
    "Its workflows were not read, so HQ cannot say which uses: lines to pin, and requiring "
    "pinning first would stop every run that uses a tag."
)
# Said once on a pinning plan where a repository calls another's workflows.
# Which workflows is the list of steps under it.
PINS_CALLED = (
    "Requiring pinning is not offered for a repository that calls another repository's "
    "workflow, because HQ did not read what that workflow uses."
)


def help_for(check: _Check, missing: list[Repository]) -> Any:
    """The item's workflow: each repository's exact commands, or why there are none."""

    key = f"github-posture:{check.id}"
    derive = COMMANDS.get(check.id)
    if derive is None:
        return cannot_help(key, REASONS[check.id])
    runs = tuple(run_step(label, command) for repo in missing for label, command in derive(repo))
    return steps(key, runs + _called_workflows(check, missing), reason=_left_out(check, missing))


def _called_workflows(check: _Check, missing: list[Repository]) -> tuple[Any, ...]:
    """One step for each workflow a repository calls from another: the thing
    to check before requiring pinning there."""

    if check.id != "actions-pinned":
        return ()
    return tuple(
        do_step(f"{repo.short}: check that {workflow} pins its own actions, then require pinning.")
        for repo in missing
        for workflow in calls_workflows(repo)
    )


def _left_out(check: _Check, missing: list[Repository]) -> str:
    """What the commands do not cover, and why: only pinning can leave some out."""

    if check.id != "actions-pinned":
        return ""
    unread = [repo.short for repo in missing if pins_unread(repo)]
    unresolved = [f"{repo.short} ({', '.join(pins_unresolved(repo))})" for repo in missing if pins_unresolved(repo)]
    called = any(calls_workflows(repo) for repo in missing)
    parts = []
    if unread:
        parts.append(f"{', '.join(unread)}: {PINS_UNREAD}")
    if unresolved:
        parts.append(
            f"Pin these by hand; their tags could not be read, so pinning is not required yet: {'; '.join(unresolved)}."
        )
    if called:
        parts.append(PINS_CALLED)
    return " ".join(parts)


def posture_of(repo: Repository) -> Posture:
    return measure(repo, STANDARD)


def postures() -> list[Posture]:
    return [posture_of(repo) for repo in sorted(repositories().values(), key=lambda item: item.name)]


def attention() -> tuple[Insight, ...]:
    """One item per check not met, naming every repository that misses it, so
    the queue grows with the gaps in the standard rather than with the estate."""

    found = postures()
    items = []
    for check in STANDARD:
        unmet = [item.subject for item in found if item.state_of(check.id) == UNMET]
        missing = [repo.short for repo in unmet]
        if not missing:
            continue
        items.append(
            Insight(
                status="serious" if check.serious else "attention",
                eyebrow="Repo checks",
                family="Repository checks",
                key=f"github-posture:{check.id}",
                title=f"{check.label}: not met in {counted(len(missing), 'repository', 'repositories')}",
                value=str(len(missing)),
                # A setting to change in each repository that misses it.
                magnitude=len(missing),
                body=f"{', '.join(missing)}. {check.fix}",
                action="Open repo checks",
                url=reverse("posture"),
                workflow=help_for(check, unmet),
            )
        )
    return tuple(items)


def build_attention() -> tuple[Insight, ...]:
    """What GitHub holds for you: the repositories' own items, then the standard's."""

    return repository_attention() + attention()
