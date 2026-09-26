"""GitHub metadata gateway for project refreshes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from django.conf import settings
from django.urls import reverse

from application.connection_contracts import (
    ConnectionAbility,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from application.security import Capability


class GitHubMetadataError(RuntimeError):
    """GitHub metadata could not be fetched or parsed."""


def connection_specs():
    """Emit the GitHub gateway's safe connection and executable process."""

    def instances():
        token_configured = bool(getattr(settings, "GITHUB_API_TOKEN", "").strip())
        return (
            ConnectionInstance(
                id="github-api",
                label="GitHub",
                kind="github",
                status="good" if token_configured else "neutral",
                status_label="authenticated" if token_configured else "public access",
                detail=(
                    "Token set. Health is checked when an operation runs."
                    if token_configured
                    else "No token. Uses GitHub's anonymous rate limit."
                ),
                endpoint="https://api.github.com",
                # A personal token is whatever its owner made it, and HQ does
                # not read its permissions; what HQ asks of it needs none.
                credential_model="coarse" if token_configured else "none",
                ability_names=("github.repository_metadata",),
                targets=(ConnectionLink("Registered projects", reverse("projects:list")),),
            ),
        )

    return (
        ConnectionSpec(
            name="hq.github",
            label="GitHub",
            summary="Repository metadata for registered projects.",
            required_capability=Capability.READ,
            instance_provider=instances,
            abilities=(
                ConnectionAbility(
                    name="github.repository_metadata",
                    label="Refresh repository metadata",
                    summary="Read the last push time for a registered project.",
                    effect="remote_write",
                    # Public repository metadata needs no grant; a token only
                    # lifts the anonymous rate limit.
                    grant="none",
                    capability="project.refresh",
                    subject_resource="projects",
                ),
            ),
            web_route="projects:list",
            management_route="projects:list",
            documentation_url="https://docs.github.com/en/rest/repos/repos",
            secret_store="Deployment secrets",
        ),
    )


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


def fetch_last_push(
    repository_url: str,
    *,
    token: str = "",
    timeout: int = 10,
) -> datetime | None:
    found = github_repository(repository_url)
    if found is None:
        raise GitHubMetadataError("Project repository URL must identify a GitHub repository.")
    owner, repository = found

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
        # An HTTPError is the error response itself, socket included. Chained
        # below it would stay open until the GitHubMetadataError was collected.
        exc.close()
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
