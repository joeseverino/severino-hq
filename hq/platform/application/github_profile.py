"""Your GitHub profile, and what you watch there, as the controller last read it.

Whose profile is the login the signed-in person's sign-in claims, never one
typed into HQ. HQ reads nothing here: the controller reads GitHub's public API
(``controller/providers/github_profile.go``), under the GitHub App's allowance
where there is one and anonymously where not, on a clock of its own since
GitHub rations calls by the hour (``observations.github.PROFILE_EVERY``).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import PROFILE_EVERY, PROFILE_KIND as KIND
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog, LinkedAccount

from .asks import Standing, read_standing
from .command_fields import Describe
from .linked_accounts import GITHUB
from .record_errors import RecordNotFound
from .security import Principal
lazy from .cadence import forced_reads, request_reads
lazy from .timestamps import moment

# GitHub's allowance is by the hour, so a read that failed is tried again after one.
RETRY_AFTER = timedelta(hours=1)

READ_NOW = "github.profile.read"


@dataclass(frozen=True, slots=True)
class ProfileReadCommand:
    login: Annotated[str, Describe("The GitHub login whose profile to read.")]


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
    return tuple(str(record.get("login", "")) for record in (snapshot.records if snapshot is not None else ()))


def accounts() -> tuple[str, ...]:
    """Every GitHub account a sign-in names, once each."""

    return tuple(
        sorted(set(LinkedAccount.objects.filter(provider=GITHUB).exclude(login="").values_list("login", flat=True)))
    )


def asked_at() -> datetime | None:
    """When a read of the profile was asked for and has not been tried yet."""

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
    behind = now - snapshot.observed_at >= PROFILE_EVERY or any(login.lower() not in held for login in wanted)
    return {"accounts": list(wanted), "due": behind and now - snapshot.updated_at >= RETRY_AFTER}


def standing() -> Standing:
    """How a read somebody asked for stands; idle when nobody is waiting on one."""

    asked = asked_at()
    return read_standing((KIND,), asked) if asked is not None else Standing()


def request_read(command: ProfileReadCommand, *, principal: Principal) -> dict[str, Any]:
    """Ask the controller to read a profile now: the ``github.profile.read``
    capability, answering when it was asked.

    A read spends GitHub's hourly allowance and says what HQ is asking about,
    so asking for one is gated like every other public lookup, and waking the
    controller is gated as Refresh is.
    """

    del principal
    if command.login not in accounts():
        raise RecordNotFound("No sign-in names that GitHub account.")
    request_reads((KIND,))
    record_event(
        action=AuditLog.Action.UPDATED,
        type_label="GitHub profile",
        message=f"Asked for @{command.login}'s GitHub profile to be read now.",
        metadata={"login": command.login},
        required=True,
    )
    return {"ok": True, "login": command.login, "asked_at": (asked_at() or timezone.now()).isoformat()}
