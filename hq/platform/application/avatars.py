"""A person's picture, fetched from their identity provider when they sign in.

The ``picture`` claim is an address HQ does not fetch on trust: only the
provider's own origin is asked (``picture_allowed``, ``fetch_picture``). What
comes back is kept and served by HQ, so a page needs nothing from another
origin to draw it. Nothing here may stop a sign-in: a picture that cannot be
fetched is a picture that is not shown.
"""

import hashlib
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx2
from django.db import transaction
from django.utils import timezone

from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog, Avatar

logger = logging.getLogger("severino.auth")

AUDIT_LABEL = "Avatar"
# Where a session says which picture its person has: the picture's digest.
SESSION_KEY = "avatar"
# A profile picture, not a photograph: larger than this is not one.
MAX_BYTES = 512 * 1024
# A session is renewed every few minutes and each renewal is a sign-in. The
# picture is asked for again only once it is this old.
REFRESH_AFTER = timedelta(days=1)
_TIMEOUT = httpx2.Timeout(5, connect=3)
# What the file starts with, not what the response says it is. SVG is absent
# on purpose: it is a document that can carry script.
_KINDS = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _kind(image: bytes) -> str:
    for start, kind in _KINDS:
        if image.startswith(start):
            return kind
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def _at_most(chunks: Iterator[bytes], limit: int) -> bytes:
    """The body's first ``limit`` bytes, reading no further than it takes."""

    body = b""
    for chunk in chunks:
        body += chunk
        if len(body) >= limit:
            return body[:limit]
    return body


def picture_allowed(url: str, issuer: str) -> bool:
    """Whether ``url`` is the provider's own: same scheme, host and port."""

    if not url or not issuer:
        return False
    scheme, host, _port = _origin(url)
    return scheme in ("https", "http") and bool(host) and _origin(url) == _origin(issuer)


def fetch_picture(url: str, *, access_token: str = "") -> tuple[str, bytes] | None:
    """The picture at ``url`` as (content type, bytes), or None.

    The provider's token is offered, since the picture is its own resource and
    may be private to the person it shows. No redirect is followed: one would
    take the request somewhere the origin check never saw.
    """

    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    try:
        with httpx2.stream("GET", url, headers=headers, timeout=_TIMEOUT, follow_redirects=False) as response:
            if response.status_code != 200:
                return None
            image = _at_most(response.iter_bytes(), MAX_BYTES + 1)
    except httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError, OSError, ValueError:
        return None
    if not image or len(image) > MAX_BYTES:
        return None
    kind = _kind(image)
    return (kind, image) if kind else None


def remember_avatar(user: Any, url: str, *, issuer: str, access_token: str = "", now: datetime | None = None) -> str:
    """Keep ``user``'s picture current, and return its digest ("" for none).

    A provider that names no picture has none to show: one HQ kept earlier is
    dropped, because the person removed it. One that names a picture HQ cannot
    fetch just now leaves the kept one alone.
    """

    now = now or timezone.now()
    kept = Avatar.objects.filter(user=user).only("digest", "source", "fetched_at").first()
    if not url:
        if kept is not None:
            _forget(user)
        return ""
    if not picture_allowed(url, issuer):
        logger.warning(
            "A picture claim outside the provider's origin was not fetched.",
            extra={"event": "auth.avatar.refused"},
        )
        return kept.digest if kept else ""
    if kept is not None and kept.source == url and now - kept.fetched_at < REFRESH_AFTER:
        return kept.digest
    fetched = fetch_picture(url, access_token=access_token)
    if fetched is None:
        return kept.digest if kept else ""
    kind, image = fetched
    digest = hashlib.sha256(image).hexdigest()
    with transaction.atomic():
        avatar, created = Avatar.objects.update_or_create(
            user=user,
            defaults={
                "content_type": kind,
                "image": image,
                "digest": digest,
                "source": url,
                "fetched_at": now,
            },
        )
        if created or kept is None or kept.digest != digest:
            record_event(
                action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
                obj=avatar,
                type_label=AUDIT_LABEL,
                message="Picture taken from your sign-in service when you signed in",
                user=user,
            )
    return digest


def _forget(user: Any) -> None:
    with transaction.atomic():
        avatar = Avatar.objects.filter(user=user).only("id", "digest").first()
        if avatar is None:
            return
        record_event(
            action=AuditLog.Action.DELETED,
            obj=avatar,
            type_label=AUDIT_LABEL,
            message="Your sign-in service no longer gives a picture",
            user=user,
        )
        avatar.delete()


def avatar_of(user: Any) -> Avatar | None:
    """The picture HQ keeps for ``user``, bytes and all, to serve it."""

    return Avatar.objects.filter(user=user).first()
