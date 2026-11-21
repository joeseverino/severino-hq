"""The standard your repositories are held to, and whether each meets it.

Every repository is held to what protects code wherever it lives; a public one
to more, because GitHub gives public repositories more (scanning, push
protection, rules on the default branch), and what a plan does not offer is
"not available", never a failure. Checked against the GitHub App's own reading
(``github_estate``), so nothing here asks GitHub anything.
"""

import json
import shlex
from collections.abc import Callable
from datetime import timedelta
from typing import Any

lazy from django.conf import settings

from .derivations import passed
from .github_estate import Repository, repositories
from .moments import moment
from .standards import Check as _Check, Examples, Measurement, Reading, measure
from .ui import ended
lazy from .projects import repository_key

BOTH = "both"
PUBLIC = "public"
# A deploy key nobody has used in this long is either dead weight or moved.
KEY_IDLE_DAYS = 90


def _public(repo: Repository) -> bool:
    return not repo.private


def Check(id: str, label: str, scope: str, test, why: str, fix: str, *, serious: bool = False, **said: Any) -> _Check:
    """A repository check: every repository's, or a public one's besides."""

    return _Check(
        id, label, test, why, fix, serious=serious, scope=scope, applies=_public if scope == PUBLIC else None, **said
    )


ACCESS_UNREAD = "Its access has not been read"

# The secrets the host's own wiring sets (mise run wire-github-app). A
# variable by one of these names is one of them kept in the wrong place.
APP_KEY, APP_CLIENT_ID = "HQ_APP_KEY", "HQ_APP_CLIENT_ID"
WIRED_SECRETS = frozenset({APP_KEY, APP_CLIENT_ID})
# The owner, the one collaborator a kept repository has.
_OWNER = {"login": "example", "role": "admin"}


def _access(repo: Repository) -> dict[str, Any] | None:
    return repo.record.get("access")


def _on_access(test: Callable[[Repository, dict[str, Any]], bool]) -> Callable[[Repository], Reading]:
    """A check of the access reading, unavailable when it was not read."""

    def check(repo: Repository) -> Reading:
        access = _access(repo)
        return Reading.unavailable(ACCESS_UNREAD) if access is None else Reading.judged(test(repo, access))

    return check


def _only_you(repo: Repository, access: dict[str, Any]) -> bool:
    owner = repo.name.split("/", 1)[0].lower()
    return all(item["login"].lower() == owner for item in access["collaborators"])


def _keys_read_only(_repo: Repository, access: dict[str, Any]) -> bool:
    return all(key["read_only"] for key in access["deploy_keys"])


def _idle(key: dict[str, Any]) -> bool:
    seen = moment(key.get("last_used") or key.get("created_at") or "")
    return seen is not None and passed(seen + timedelta(days=KEY_IDLE_DAYS))


def _keys_in_use(_repo: Repository, access: dict[str, Any]) -> bool:
    return not any(_idle(key) for key in access["deploy_keys"])


def _setting(name: str, *, expected: Any) -> Callable[[Repository], Reading]:
    return _on_access(lambda _repo, access: access.get(name) == expected)


# A variable is public configuration, printed unmasked; one named for a
# credential is a secret kept in the wrong place. A name that says it is public
# (a site key, a publishable key) is what it says.
_SECRET_WORDS = frozenset({"TOKEN", "SECRET", "PASSWORD", "PASSPHRASE", "PRIVATE", "CREDENTIAL", "CREDENTIALS", "KEY"})
_PUBLIC_WORDS = frozenset({"PUBLIC", "PUBLISHABLE"})


def _secret_named(name: str) -> bool:
    words = set(name.upper().replace("-", "_").split("_"))
    return name in WIRED_SECRETS or (bool(words & _SECRET_WORDS) and not words & _PUBLIC_WORDS)


def _secrets_in_variables(repo: Repository) -> tuple[str, ...]:
    return tuple(name for name in repo.record.get("variables") or () if _secret_named(name))


def _no_secret_variables(repo: Repository) -> Reading:
    if repo.record.get("variables") is None:
        return Reading.unavailable("Its Actions variables have not been read")
    return Reading.judged(not _secrets_in_variables(repo))


def _rule(name: str) -> Callable[[Repository], Reading]:
    def test(repo: Repository) -> Reading:
        rules = repo.record.get("rules")
        return (
            Reading.unavailable("Its rulesets have not been read")
            if rules is None
            else Reading.judged(bool(rules.get(name)))
        )

    return test


def _security(name: str) -> Callable[[Repository], Reading]:
    def test(repo: Repository) -> Reading:
        security = (_access(repo) or {}).get("security")
        if not security:
            return Reading.unavailable("Its security settings have not been read")
        return Reading.judged(security.get(name) == "enabled")

    return test


def _code_scanning(repo: Repository) -> Reading:
    if "code_scanning" in (repo.record.get("alerts") or {}):
        return Reading.met()
    return Reading.unavailable("No code scanning alerts have been read")


STANDARD: tuple[Check, ...] = (
    Check(
        "only-you",
        "Only you have access",
        BOTH,
        _on_access(_only_you),
        "Anyone else with access can read private code or push to it.",
        "Remove the collaborator in the repository's access settings.",
        serious=True,
        unmet="Others have access",
        breaks=({"access": {"collaborators": [_OWNER, {"login": "guest", "role": "write"}]}},),
    ),
    Check(
        "keys-read-only",
        "Deploy keys can only read",
        BOTH,
        _on_access(_keys_read_only),
        "A deploy key that can write can push to the repository without you.",
        "Replace the key with a read-only one.",
        serious=True,
        unmet="A deploy key can write",
        breaks=({"access": {"deploy_keys": [{"title": "deploy", "read_only": False}]}},),
    ),
    Check(
        "keys-in-use",
        "Every deploy key is in use",
        BOTH,
        _on_access(_keys_in_use),
        f"A key unused for {KEY_IDLE_DAYS} days is access nothing needs.",
        "Delete the key, or find what stopped using it.",
        unmet=f"A deploy key has gone unused for {KEY_IDLE_DAYS} days",
        breaks=({"access": {"deploy_keys": [{"title": "deploy", "read_only": True, "last_used": "2000-01-01"}]}},),
    ),
    Check(
        "token-read-only",
        "The workflow token is read-only",
        BOTH,
        _setting("token", expected="read"),
        "A writable default token lets any workflow change the repository.",
        "Set workflow permissions to read in the Actions settings.",
        unmet="The workflow token can write",
        breaks=({"access": {"token": "write"}},),
    ),
    Check(
        "token-no-approvals",
        "Workflows cannot approve pull requests",
        BOTH,
        _setting("token_approves_reviews", expected=False),
        "A workflow that approves its own pull request skips review.",
        "Turn off approving pull requests in the Actions settings.",
        unmet="Workflows can approve pull requests",
        breaks=({"access": {"token_approves_reviews": True}},),
    ),
    Check(
        "actions-pinned",
        "Actions must be pinned to a commit",
        BOTH,
        _setting("pinning_required", expected=True),
        "A tag can be moved to different code; a commit cannot.",
        "Pin every action its workflows use to a commit, then require pinning in the Actions settings.",
        unmet="Actions need not be pinned to a commit",
        breaks=({"access": {"pinning_required": False}},),
    ),
    Check(
        "vulnerability-alerts",
        "Vulnerability alerts are on",
        BOTH,
        _setting("vulnerability_alerts", expected=True),
        "Renovate opens its security fixes from these alerts; without them a vulnerable dependency stays until you notice it.",
        "Turn on Dependabot alerts in the security settings.",
        unmet="Vulnerability alerts are off",
        breaks=({"access": {"vulnerability_alerts": False}},),
    ),
    Check(
        "no-secret-variables",
        "No secret is kept in an Actions variable",
        BOTH,
        _no_secret_variables,
        "A variable's value is printed unmasked in workflow logs; a secret's is masked.",
        "Copy it into a secret of the same name, then delete the variable once its workflows read the secret.",
        unmet="A secret is kept in an Actions variable",
        breaks=({"variables": ["DEPLOY_TOKEN"]}, {"variables": [APP_CLIENT_ID]}),
        keeps=({"variables": ["DEPLOY_TARGET", "PUBLIC_SITE_KEY"]},),
    ),
    Check(
        "pull-request-required",
        "Changes arrive by pull request",
        PUBLIC,
        _rule("pull_request"),
        "A direct push to the default branch skips every check.",
        "Require a pull request in the default branch's ruleset.",
        unmet="Changes can be pushed without a pull request",
        breaks=({"rules": {"pull_request": False}},),
    ),
    Check(
        "force-push-blocked",
        "The default branch cannot be rewritten",
        PUBLIC,
        _rule("blocks_force_push"),
        "A force push rewrites history others have already built on.",
        "Block force pushes in the default branch's ruleset.",
        unmet="The default branch can be rewritten",
        breaks=({"rules": {"blocks_force_push": False}},),
    ),
    Check(
        "deletion-blocked",
        "The default branch cannot be deleted",
        PUBLIC,
        _rule("blocks_deletion"),
        "Deleting the default branch takes every deploy with it.",
        "Block deletion in the default branch's ruleset.",
        unmet="The default branch can be deleted",
        breaks=({"rules": {"blocks_deletion": False}},),
    ),
    Check(
        "secret-scanning",
        "Leaked credentials are scanned for",
        PUBLIC,
        _security("secret_scanning"),
        "A credential committed to a public repository is found by others first.",
        "Turn on secret scanning.",
        unmet="Leaked credentials are not scanned for",
        breaks=({"access": {"security": {"secret_scanning": "disabled"}}},),
    ),
    Check(
        "push-protection",
        "Credentials are stopped before they are pushed",
        PUBLIC,
        _security("secret_scanning_push_protection"),
        "Scanning finds a leak after it is public; push protection stops it.",
        "Turn on push protection.",
        unmet="Credentials are not stopped before they are pushed",
        breaks=({"access": {"security": {"secret_scanning_push_protection": "disabled"}}},),
    ),
    Check(
        "code-scanning",
        "Code is scanned",
        PUBLIC,
        _code_scanning,
        "Code scanning finds vulnerable patterns before a reviewer does.",
        "Turn on code scanning.",
        unmet="Code is not scanned",
        breaks=(),
    ),
)


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
            (
                f"{repo.short}: delete the writable key {key['title']}, then add its public key back "
                f"with gh repo deploy-key add -R {repo.name}, which is read-only unless told otherwise"
            ),
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
    """Each secret kept as a variable moves into a secret first, and the variable
    goes only once the workflows read the secret, so no step breaks a build."""

    runs: list[tuple[str, str]] = []
    for name in _secrets_in_variables(repo):
        delete = f"gh variable delete {name} -R {repo.name}"
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

    Each tag is pinned to the commit it names now, as the App read it.
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
            # Perl rather than sed, because ``sed -i`` takes its arguments
            # differently on macOS and on Linux.
            script = shlex.quote(" ".join(edits))
            runs.append((f"{repo.short}: pin {path}", f"perl -pi -e {script} {shlex.quote(path)}"))
    # Requiring pinning before the workflows are pinned stops every run, and it
    # applies as much to what a called workflow uses, which HQ did not read.
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
    "vulnerability-alerts": _setting(path="/vulnerability-alerts"),
    "no-secret-variables": _variables,
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
    "HQ has not read its workflows, so it cannot list the uses: lines to pin. "
    "Requiring pinning first would stop every run that uses a tag."
)
# Said once on a pinning plan where a repository calls another's workflows.
# Which workflows is the list of steps under it.
PINS_CALLED = "It calls another repository's workflow, which HQ has not read, so HQ does not offer to require pinning."


def left_out(check: _Check, missing: list[Repository]) -> str:
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
            ended(
                f"HQ could not read these tags, so pinning is not required yet. Pin them by hand: {'; '.join(unresolved)}"
            )
        )
    if called:
        parts.append(PINS_CALLED)
    return " ".join(parts)


# Why a repository whose only unpinned lines call HQ's own workflows is left
# unpinned: requiring pinning would refuse them.
FOLLOWS_HOST = "It calls HQ's own workflows at their main branch, which requiring pinning would refuse."


def _host() -> str:
    """``owner/name`` of the repository HQ is built from; "" when it says none."""

    return repository_key(settings.SEVERINO_HQ_SOURCE)


def _intended(repo: Repository) -> dict[str, str]:
    """Checks a repository misses on purpose: pinning, when every line left
    unpinned names a workflow or action of the host's own repository."""

    host = _host()
    pins = repo.record.get("pins") or ()
    if not host or repo.name.lower() == host or not pins:
        return {}
    if all(str(pin.get("uses", "")).lower().startswith(f"{host}/") for pin in pins):
        return {"actions-pinned": FOLLOWS_HOST}
    return {}


def posture_of(repo: Repository) -> Measurement | None:
    """How ``repo`` meets the standard; None for an archived one, which is
    read-only and so has nothing more to protect."""

    return None if repo.archived else measure(repo, STANDARD, intended=_intended(repo))


def _offered(repo: Repository, check: _Check) -> bool:
    derive = COMMANDS.get(check.id)
    return bool(derive and derive(repo)) or bool(REASONS.get(check.id) or left_out(check, [repo]))


# A kept public repository: only its owner, one read-only key, a read-only
# token that approves nothing, pinned actions, fixes and scanning on, and its
# default branch protected.
EXAMPLES = Examples(
    checks=STANDARD,
    subject=lambda record: Repository("example/alpha", record, None),
    kept={
        "private": False,
        "access": {
            "collaborators": [_OWNER],
            "deploy_keys": [{"title": "deploy", "read_only": True}],
            "token": "read",
            "token_approves_reviews": False,
            "pinning_required": True,
            "vulnerability_alerts": True,
            "security": {"secret_scanning": "enabled", "secret_scanning_push_protection": "enabled"},
        },
        "variables": [],
        "rules": {"pull_request": True, "blocks_force_push": True, "blocks_deletion": True},
        "alerts": {"code_scanning": {}},
    },
    unread=({}, {"private": False}),
    fixed=_offered,
)


def postures() -> list[Measurement]:
    found = (posture_of(repo) for repo in sorted(repositories().values(), key=lambda item: item.name))
    return [posture for posture in found if posture is not None]
