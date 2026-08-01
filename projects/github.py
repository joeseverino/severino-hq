"""GitHub metadata gateway for project refreshes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse


class GitHubMetadataError(RuntimeError):
    """GitHub metadata could not be fetched or parsed."""


def fetch_last_push(
    repository_url: str,
    *,
    token: str = "",
    timeout: int = 10,
) -> datetime | None:
    parsed = urlparse(repository_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or len(parts) != 2
    ):
        raise GitHubMetadataError("Project repository URL must identify a GitHub repository.")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not owner or not repository:
        raise GitHubMetadataError("Project repository URL must identify a GitHub repository.")

    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repository}",
        headers={
            "Accept": "application/vnd.github.v3+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise GitHubMetadataError(f"GitHub API returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise GitHubMetadataError(f"Could not fetch GitHub metadata: {exc}") from exc

    pushed_at = payload.get("pushed_at")
    if not pushed_at:
        return None
    try:
        return datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise GitHubMetadataError("GitHub returned an invalid pushed_at timestamp.") from exc
