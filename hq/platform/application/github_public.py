"""Reading GitHub's public API: no credential, a short timeout, plain errors.

Shared by everything HQ reads about public GitHub data from the web process
(Watching, the release behind a running build). A credentialed read is the
controller's (``control_plane.provider_adapters.github_app``), never this.
"""

import json
import urllib.error
import urllib.request
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import AfterValidator

API = "https://api.github.com"
TIMEOUT_SECONDS = 10
JSON = "application/vnd.github+json"


class GitHubReadError(Exception):
    """GitHub could not be read, in words a person can act on."""


def get(path: str, *, accept: str = JSON, missing_ok: bool = False, token: str = "") -> Any:
    """``path`` from GitHub's API. ``token`` only where a deployment gives one
    to raise the rate limit on public data; nothing here needs it."""

    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Severino-HQ"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310: fixed https host
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # An HTTPError is the error response itself, socket included. Chained
        # below the error raised here it would stay open until that was collected.
        exc.close()
        if missing_ok and exc.code == 404:
            return None
        if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubReadError(
                "GitHub's hourly limit for anonymous reads is spent. The last read stays until it resets."
            ) from exc
        raise GitHubReadError(f"GitHub API returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise GitHubReadError(f"Could not reach GitHub: {exc}") from exc


def github_repository(repository_url: str) -> tuple[str, str] | None:
    """``(owner, repository)`` when the URL names a GitHub repository, else None."""

    parsed = urlparse(str(repository_url or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or len(parts) != 2
    ):
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
