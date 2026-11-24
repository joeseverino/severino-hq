"""Your GitHub profile, and what you watch there, as the controller last read it.

Whose profile is the login the signed-in person's sign-in claims, never one
typed into HQ. HQ reads nothing here: the controller reads GitHub's public API
(``controller/providers/github_profile.go``), under the GitHub App's allowance
where there is one and anonymously where not, on a clock of its own since
GitHub rations calls by the hour (its ``every``, ``observations.github``).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import PROFILE_KIND as KIND
from hq.platform.core.models import LinkedAccount

from .asks import AskedAnswer, Standing, asked_read, read_standing
from .command_fields import Describe
from .contracts import Effect
from .freshness import RETRY_AFTER, freshness
from .integration_specs import CapabilitySpec
from .linked_accounts import GITHUB
from .quoted import Quoted
from .record_errors import RecordNotFound
from .security import Capability, Principal
lazy from .cadence import CONTROLLER_REFRESH, ask, asked_at
lazy from .capabilities import command_spec
lazy from .moments import moment

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
            # GitHub's words for it, as its owner wrote them.
            "description": Quoted(repo.get("description") or ""),
            "starred": moment(repo["starred_at"]) if repo.get("starred_at") else None,
            "release": (
                {**repo["release"], "published": moment(repo["release"].get("published_at", ""))}
                if repo.get("release")
                else None
            ),
            "advisories": [
                {
                    **item,
                    "summary": Quoted(item.get("summary") or ""),
                    "published": moment(item["published_at"]) if item.get("published_at") else None,
                }
                for item in repo.get("advisories") or ()
            ],
        }
        for repo in value.get("watched", [])
    ]
    # The page lists the newest stars, so the newest leads whatever order a reading held.
    watched.sort(key=lambda repo: repo["starred"] or datetime.min.replace(tzinfo=UTC), reverse=True)
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
    if snapshot is None or asked_at(KIND) is not None:
        return {"accounts": list(wanted), "due": True}
    now = now or timezone.now()
    held = {login.lower() for login in profiles()}
    behind = freshness(KIND, snapshot.observed_at, now).due or any(login.lower() not in held for login in wanted)
    return {"accounts": list(wanted), "due": behind and now - snapshot.updated_at >= RETRY_AFTER}


def standing() -> Standing:
    """How a read somebody asked for stands; idle when nobody is waiting on one."""

    asked = asked_at(KIND)
    return read_standing((KIND,), asked) if asked is not None else Standing()


class ProfileReadRequested(AskedAnswer):
    """A read of a GitHub profile, asked for now."""

    login: Annotated[str, Describe("The GitHub account to read, by its login.")]


def request_read(command: ProfileReadCommand, *, principal: Principal) -> ProfileReadRequested:
    """Ask the controller to read a profile now: the ``github.profile.read``
    capability, answering when it was asked.

    A read spends GitHub's hourly allowance and says what HQ is asking about,
    so asking for one is gated like every other public lookup, and waking the
    controller is gated as Refresh is.
    """

    del principal
    if command.login not in accounts():
        raise RecordNotFound("No sign-in names that GitHub account.")
    at = ask(reads=(KIND,), activity=True)
    said = command_spec(READ_NOW).requested(f"@{command.login}")
    return {**asked_read((KIND,), at, message=said).answer(), "login": command.login}


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            READ_NOW,
            "Ask HQ to read a user's GitHub profile now.",
            Effect.READ_REQUEST,
            # A public lookup that spends GitHub's allowance, and a wake of the
            # controller, as Refresh is.
            (Capability.LOOK_UP_PUBLIC_RECORDS, Capability.MANAGE_INFRASTRUCTURE),
            ProfileReadCommand,
            request_read,
            execution_notes=(
                "Refuse a login no sign-in names.",
                "Ask for the profile reading now; the controller reads GitHub with its own credentials.",
            ),
            label="Refresh a GitHub profile",
            reached_from="watching",
            verb=CONTROLLER_REFRESH.words(),
        ),
    )
