"""Reading GitHub's public API: no credential, a short timeout, plain errors.

Shared by everything HQ reads about public GitHub data from the web process
(Watching, the release behind a running build). A credentialed read is the
controller's (``control_plane.provider_adapters.github_app``), never this.
"""

import json
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import AfterValidator

from .http_read import UNREACHABLE, fetch

API = "https://api.github.com"
TIMEOUT_SECONDS = 10
JSON = "application/vnd.github+json"
# A release list or an advisory page is well under this.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GitHubReadError(Exception):
    """GitHub could not be read, in words a person can act on."""


def get(path: str, *, accept: str = JSON, missing_ok: bool = False) -> Any:
    """``path`` from GitHub's API. A redirect is followed, as GitHub answers
    for a renamed repository."""

    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Severino-HQ"}
    try:
        response, body = fetch(
            "GET",
            f"{API}{path}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            limit=MAX_RESPONSE_BYTES,
            follow_redirects=True,
        )
    except UNREACHABLE as exc:
        raise GitHubReadError(f"Could not reach GitHub: {exc}") from exc
    status = response.status_code
    if missing_ok and status == 404:
        return None
    if status in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubReadError(
            "GitHub's hourly limit for anonymous reads is spent. The last read stays until it resets."
        )
    if not response.is_success:
        raise GitHubReadError(f"GitHub API returned HTTP {status}.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise GitHubReadError("GitHub answered with more than HQ reads.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise GitHubReadError("GitHub answered with something other than JSON.") from exc


def github_repository(repository_url: str) -> tuple[str, str] | None:
    """``(owner, repository)`` when the URL names a GitHub repository, else None."""

    parsed = urlparse(str(repository_url or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or len(parts) != 2:
        return None
    owner, repository = parts[0], parts[1].removesuffix(".git")
    return (owner, repository) if owner and repository else None


def _repository_url(value: str) -> str:
    value = value.strip().removesuffix("/")
    if value and github_repository(value) is None:
        raise ValueError("Give a GitHub repository, like https://github.com/owner/repository.")
    return value


# A field holding a GitHub repository's URL, or blank.
GitHubRepositoryURL = Annotated[str, AfterValidator(_repository_url)]
