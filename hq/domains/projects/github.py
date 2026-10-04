"""A repository's last push, read from GitHub's public API without a credential.

The fallback behind the GitHub App for a repository the App is not installed
on. Not a connection of its own: it holds no credential and reads only what
GitHub publishes to anyone.
"""

from __future__ import annotations

from datetime import datetime

from hq.platform.application.github_public import GitHubReadError, get, github_repository
from hq.platform.application.timestamps import moment


class GitHubMetadataError(RuntimeError):
    """GitHub metadata could not be fetched or parsed."""


def fetch_last_push(
    repository_url: str,
    *,
    timeout: int = 10,
) -> datetime | None:
    del timeout  # the shared reader's timeout; kept so callers need not change
    found = github_repository(repository_url)
    if found is None:
        raise GitHubMetadataError("Project repository URL must identify a GitHub repository.")
    owner, repository = found
    try:
        payload = get(f"/repos/{owner}/{repository}")
    except GitHubReadError as exc:
        raise GitHubMetadataError(str(exc)) from exc

    pushed_at = payload.get("pushed_at")
    if not pushed_at:
        return None
    pushed = moment(pushed_at, naive="keep")
    if pushed is None:
        raise GitHubMetadataError("GitHub returned an invalid pushed_at timestamp.")
    return pushed
