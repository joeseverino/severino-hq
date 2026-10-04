"""Your GitHub profile, and what you watch there, from GitHub's public API.

Whose profile is the signed-in person's: the login their sign-in claims
(``application.linked_accounts``), never one typed into HQ.

Public, credential-free reads, which is what lets the web process make them
(the precedent is ``control_plane.dns_lookup``): no token travels, so there is
nothing here for the controller to hold. They go through ``application.readings``
so opening the page never waits on GitHub, a refresh reads again, and a read
that fails keeps the last one.

Anonymous calls are limited to 60 an hour, so a read is budgeted: the profile,
the stars, the avatar, then two calls for each of the newest ``WATCHED_LIMIT``
stars. One read fits the hour with room to spare; the cache keeps it to one.
"""

from __future__ import annotations

import base64
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any

from .github_public import TIMEOUT_SECONDS, get as _get
from .readings import refresh as refresh_reading
from .readings import stored
from .security import Capability, Principal

REFRESH_AFTER = timedelta(hours=6)
WATCHED_LIMIT = 15
AVATAR_MAX_BYTES = 200_000
# The same list with the moment each repository was starred.
_STARS = "application/vnd.github.star+json"


KEY_PREFIX = "github.profile:"


def reading_key(login: str) -> str:
    return f"{KEY_PREFIX}{login.lower()}"


def profile(login: str) -> dict[str, Any] | None:
    """The last profile read for ``login``, with when; None before the first."""

    if not login:
        return None
    reading = stored(reading_key(login))
    if reading is None:
        return None
    from .timestamps import moment

    value = reading.value
    watched = [
        {
            **repo,
            "starred": moment(repo["starred_at"]) if repo["starred_at"] else None,
            "release": (
                {**repo["release"], "published": moment(repo["release"]["published_at"])}
                if repo["release"]
                else None
            ),
            "advisories": [
                {**item, "published": moment(item["published_at"]) if item["published_at"] else None}
                for item in repo["advisories"]
            ],
        }
        for repo in value.get("watched", [])
    ]
    since = moment(value.get("created_at", "")) if value.get("created_at") else None
    return {**value, "watched": watched, "since": since, "observed_at": reading.observed_at}


def refresh(login: str, *, principal: Principal, force: bool = False) -> None:
    """Read ``login`` again if it is stale, or now when ``force``.

    An outbound read spends GitHub's rate limit and says what HQ is asking
    about, so it is gated like every other one.
    """

    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    refresh_reading(
        reading_key(login),
        lambda: read(login),
        older_than=timedelta(0) if force else REFRESH_AFTER,
    )


def read(login: str) -> dict[str, Any]:
    """Everything the page shows, in one read."""

    user = _get(f"/users/{urllib.parse.quote(login)}")
    stars = _get(f"/users/{urllib.parse.quote(login)}/starred?per_page=100", accept=_STARS) or []
    return {
        "login": str(user.get("login") or login),
        "name": str(user.get("name") or ""),
        "bio": str(user.get("bio") or ""),
        "url": str(user.get("html_url") or ""),
        "followers": int(user.get("followers") or 0),
        "following": int(user.get("following") or 0),
        "public_repos": int(user.get("public_repos") or 0),
        # What the account says about you publicly, for the Account section.
        "created_at": str(user.get("created_at") or ""),
        "company": str(user.get("company") or ""),
        "location": str(user.get("location") or ""),
        "website": str(user.get("blog") or ""),
        "social": str(user.get("twitter_username") or ""),
        "public_gists": int(user.get("public_gists") or 0),
        "hireable": bool(user.get("hireable")),
        "avatar": _avatar(str(user.get("avatar_url") or "")),
        "starred": len(stars),
        "watched": [_watched(star) for star in stars[:WATCHED_LIMIT]],
    }


def _watched(star: dict[str, Any]) -> dict[str, Any]:
    repo = star.get("repo") or {}
    name = str(repo.get("full_name") or "")
    release = _get(f"/repos/{name}/releases/latest", missing_ok=True) or {}
    advisories = (
        _get(
            f"/repos/{name}/security-advisories?state=published&sort=published"
            "&direction=desc&per_page=5",
            missing_ok=True,
        )
        or []
    )
    return {
        "name": name,
        "url": str(repo.get("html_url") or ""),
        "description": str(repo.get("description") or ""),
        "language": str(repo.get("language") or ""),
        "stars": int(repo.get("stargazers_count") or 0),
        "starred_at": str(star.get("starred_at") or ""),
        "release": (
            {
                "tag": str(release.get("tag_name") or ""),
                "url": str(release.get("html_url") or ""),
                "published_at": str(release.get("published_at") or ""),
            }
            if release.get("tag_name")
            else None
        ),
        "advisories": [
            {
                "id": str(item.get("cve_id") or item.get("ghsa_id") or ""),
                "severity": str(item.get("severity") or ""),
                "summary": str(item.get("summary") or ""),
                "url": str(item.get("html_url") or ""),
                "published_at": str(item.get("published_at") or ""),
            }
            for item in advisories
            if isinstance(item, dict)
        ],
    }


def _avatar(url: str) -> str:
    """The avatar as a ``data:`` URI: the page's policy loads no image from another host."""

    if not url.startswith("https://avatars.githubusercontent.com/"):
        return ""
    sized = f"{url}{'&' if '?' in url else '?'}s=96"
    try:
        with urllib.request.urlopen(sized, timeout=TIMEOUT_SECONDS) as response:  # nosec B310: fixed https host
            kind = response.headers.get_content_type()
            body = response.read(AVATAR_MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return ""
    if not kind.startswith("image/") or len(body) > AVATAR_MAX_BYTES:
        return ""
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"
