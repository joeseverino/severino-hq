"""Your GitHub profile, and what you watch there, as the controller last read it.

Whose profile is the signed-in person's: the login their sign-in claims
(``application.linked_accounts``), never one typed into HQ.

HQ reads nothing here. The controller reads GitHub's public API
(``controller/providers/github_profile.go``), under the connected GitHub App's
allowance where there is one and anonymously where there is not, and reports
the ``github.profile`` reading, which is what a page shows. GitHub rations
calls by the hour, so the reading keeps a clock of its own
(``observations.github.PROFILE_EVERY``): ``plan`` tells the controller whose
profile to read and whether it is due, and ``request_read`` asks for one now.
``observations.github.WATCHED_KEPT`` states what a read costs.
"""

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import PROFILE_EVERY, PROFILE_KIND as KIND
from hq.platform.core.models import LinkedAccount

from .asks import Standing, read_standing
from .linked_accounts import GITHUB
from .security import Capability, Principal

# GitHub's allowance is by the hour, so a read that failed is tried again after one.
RETRY_AFTER = timedelta(hours=1)


def _snapshot() -> ProviderInventory | None:
    return ProviderInventory.objects.filter(kind=KIND).first()


def _record(snapshot: ProviderInventory | None, login: str) -> dict[str, Any] | None:
    wanted = login.lower()
    return next(
        (
            record
            for record in (snapshot.records if snapshot is not None else ())
            if str(record.get("login", "")).lower() == wanted
        ),
        None,
    )


def profile(login: str) -> dict[str, Any] | None:
    """The last profile read for ``login``, with when; None before the first."""

    if not login:
        return None
    snapshot = _snapshot()
    value = _record(snapshot, login)
    if snapshot is None or value is None:
        return None
    from .timestamps import moment

    watched = [
        {
            **repo,
            "starred": moment(repo["starred_at"]) if repo.get("starred_at") else None,
            "release": (
                {**repo["release"], "published": moment(repo["release"].get("published_at", ""))}
                if repo.get("release")
                else None
            ),
            "advisories": [
                {**item, "published": moment(item["published_at"]) if item.get("published_at") else None}
                for item in repo.get("advisories") or ()
            ],
        }
        for repo in value.get("watched", [])
    ]
    since = moment(value["created_at"]) if value.get("created_at") else None
    return {**value, "watched": watched, "since": since, "observed_at": snapshot.observed_at}


def profiles() -> tuple[str, ...]:
    """The logins a profile is held for."""

    snapshot = _snapshot()
    return tuple(
        str(record.get("login", "")) for record in (snapshot.records if snapshot is not None else ())
    )


def accounts() -> tuple[str, ...]:
    """Every GitHub account a sign-in names, once each."""

    return tuple(
        sorted(
            set(
                LinkedAccount.objects.filter(provider=GITHUB)
                .exclude(login="")
                .values_list("login", flat=True)
            )
        )
    )


def asked_at() -> datetime | None:
    """When a read of the profile was asked for and has not been tried yet."""

    from .cadence import forced_reads

    return next((read.requested_at for read in forced_reads() if read.kind == KIND), None)


def plan(now: datetime | None = None) -> dict[str, Any]:
    """Whose profile the controller reads, and whether to read now.

    Due when somebody asked, or when the reading is older than its clock allows
    or lacks an account and the last attempt is an allowance window old. A read
    that failed or was refused was an attempt, so it is not made again on every
    sweep. Otherwise the controller carries the kind and spends no call.
    """

    wanted = accounts()
    if not wanted:
        return {"accounts": [], "due": False}
    snapshot = _snapshot()
    if snapshot is None or asked_at() is not None:
        return {"accounts": list(wanted), "due": True}
    now = now or timezone.now()
    held = {login.lower() for login in profiles()}
    behind = now - snapshot.observed_at >= PROFILE_EVERY or any(
        login.lower() not in held for login in wanted
    )
    return {"accounts": list(wanted), "due": behind and now - snapshot.updated_at >= RETRY_AFTER}


def standing() -> Standing:
    """How a read somebody asked for stands; idle when nobody is waiting on one."""

    asked = asked_at()
    return read_standing((KIND,), asked) if asked is not None else Standing()


def request_read(login: str, *, principal: Principal) -> datetime:
    """Ask the controller to read ``login``'s profile now: when it was asked.

    A read spends GitHub's hourly allowance and says what HQ is asking about,
    so asking for one is gated like every other public lookup, and waking the
    controller is gated as Read now is.
    """

    from .cadence import request_reads

    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    if login not in accounts():
        raise ValueError("No sign-in names that GitHub account.")
    request_reads((KIND,), principal=principal)
    return asked_at() or timezone.now()
