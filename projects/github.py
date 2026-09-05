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
                    "Token configured; external health is established only when a "
                    "registered operation runs."
                    if token_configured
                    else "Registered public repositories use GitHub's anonymous API limits."
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
            summary="Repository metadata used by registered HQ projects.",
            required_capability=Capability.READ,
            instance_provider=instances,
            abilities=(
                ConnectionAbility(
                    name="github.repository_metadata",
                    label="Refresh repository metadata",
                    summary="Read the latest push metadata for a registered project.",
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
