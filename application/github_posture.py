"""The standard your repositories are held to, and whether each meets it.

Two standards from one list. Every repository is held to what protects code
wherever it lives (who has access, what keys can do, how Actions may run); a
public one is held to more, because GitHub gives public repositories more:
scanning, push protection, rules on the default branch. What a plan does not
offer is "not available", never a failure.

Checked against the GitHub App's own reading (``github_estate``), so nothing
here asks GitHub anything, and a check HQ cannot read says so.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from django.urls import reverse
from django.utils import timezone

from .github_estate import Repository, repositories
from .github_estate import attention as repository_attention
from .standards import UNMET, Posture, measure
from .standards import Check as _Check
from .ui import Insight, counted, moment

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


def _keys_in_use(repo: Repository) -> bool | None:
    access = _access(repo)
    if access is None:
        return None
    cutoff = timezone.now() - timedelta(days=KEY_IDLE_DAYS)
    for key in access["deploy_keys"]:
        seen = moment(key.get("last_used") or key.get("created_at") or "")
        if seen is not None and seen < cutoff:
            return False
    return True


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
    Check("only-you", "Only you have access", BOTH, _only_you,
          "Anyone else with access can read private code or push to it.",
          "Remove the collaborator in the repository's access settings.", serious=True),
    Check("keys-read-only", "Deploy keys can only read", BOTH, _keys_read_only,
          "A deploy key that can write can push to the repository without you.",
          "Replace the key with a read-only one.", serious=True),
    Check("keys-in-use", "Every deploy key is in use", BOTH, _keys_in_use,
          f"A key unused for {KEY_IDLE_DAYS} days is access nothing needs.",
          "Delete the key, or find what stopped using it."),
    Check("token-read-only", "The workflow token is read-only", BOTH, _setting("token", "read"),
          "A writable default token lets any workflow change the repository.",
          "Set workflow permissions to read in the Actions settings."),
    Check("token-no-approvals", "Workflows cannot approve pull requests", BOTH, _setting("token_approves_reviews", False),
          "A workflow that approves its own pull request skips review.",
          "Turn off approving pull requests in the Actions settings."),
    Check("actions-pinned", "Actions must be pinned to a commit", BOTH, _setting("pinning_required", True),
          "A tag can be moved to different code; a commit cannot.",
          "Require actions to be pinned to a full-length commit SHA in the Actions settings."),
    Check("security-fixes", "Dependabot opens security fixes", BOTH, _setting("security_fixes", True),
          "A known-vulnerable dependency waits for you to notice it.",
          "Turn on Dependabot security updates."),
    Check("no-variables", "No Actions variables", BOTH, _no_variables,
          "A variable is state outside the repository that changes what a build does.",
          "Move it into the workflow, or delete it."),
    Check("pull-request-required", "Changes arrive by pull request", PUBLIC, _rule("pull_request"),
          "A direct push to the default branch skips every check.",
          "Require a pull request in the default branch's ruleset."),
    Check("force-push-blocked", "The default branch cannot be rewritten", PUBLIC, _rule("blocks_force_push"),
          "A force push rewrites history others have already built on.",
          "Block force pushes in the default branch's ruleset."),
    Check("deletion-blocked", "The default branch cannot be deleted", PUBLIC, _rule("blocks_deletion"),
          "Deleting the default branch takes every deploy with it.",
          "Block deletion in the default branch's ruleset."),
    Check("secret-scanning", "Leaked credentials are scanned for", PUBLIC, _security("secret_scanning"),
          "A credential committed to a public repository is found by others first.",
          "Turn on secret scanning."),
    Check("push-protection", "Credentials are stopped before they are pushed", PUBLIC, _security("secret_scanning_push_protection"),
          "Scanning finds a leak after it is public; push protection stops it.",
          "Turn on push protection."),
    Check("code-scanning", "Code is scanned", PUBLIC, _code_scanning,
          "Code scanning finds vulnerable patterns before a reviewer does.",
          "Turn on code scanning."),
)


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
        missing = [item.subject.short for item in found if item.state_of(check.id) == UNMET]
        if not missing:
            continue
        items.append(
            Insight(
                status="serious" if check.serious else "attention",
                eyebrow="Posture",
                key=f"github-posture:{check.id}",
                title=f"{check.label}: not met in {counted(len(missing), 'repository', 'repositories')}",
                value=str(len(missing)),
                # A setting to change in each repository that misses it.
                magnitude=len(missing),
                body=f"{', '.join(missing)}. {check.fix}",
                action="Open posture",
                url=reverse("posture"),
            )
        )
    return tuple(items)


def build_attention() -> tuple[Insight, ...]:
    """What GitHub holds for you: the repositories' own items, then the standard's."""

    return repository_attention() + attention()
