"""Findings about the secrets a machine renders, from its ``host.render_status`` reading.

Cached files keep everything running while a renderer fails, so nothing else
notices: a rotated credential never arrives and a revoked one keeps working.
The reading and each finding: docs/Secrets.md#how-hq-reads-it.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings

from hq.domains.control_plane.observations.host import (
    RENDER_READ,
    RENDER_STATUS_KIND,
    RENDER_UNREADABLE,
)

from .finding_model import (
    Finding,
    FindingEstate,
    FindingRule,
    cannot_run_commands,
    journal_step,
    machine_step,
)
from .finding_steps import Do, Run
from .freshness import missed
from .moments import duration, moment, since_when, when
from .projection import iso
from .reading_facts import ReadingRow, stated, unjudged
from .tone import Tone
from .topology_model import TopologyNode
from .ui import ended

# The renderer's timer starts it every OnUnitActiveSec, up to
# RandomizedDelaySec late, read from the timer itself so the two cannot
# disagree. A run that finds the vault unchanged still reads it in full once a
# day (FullEvery in controller/cmd/hq-secrets/main.go, which
# ``test_render_status`` holds to the value below).
RENDER_TIMER = Path(settings.BASE_DIR) / "deploy" / "systemd" / "severino-hq-secrets.timer"
_SPAN = re.compile(r"(\d+)\s*(h|min|s)")
_UNITS = {"h": timedelta(hours=1), "min": timedelta(minutes=1), "s": timedelta(seconds=1)}


def _timer_span(key: str) -> timedelta:
    """One time span the renderer's timer declares, in systemd's notation."""

    for line in RENDER_TIMER.read_text().splitlines():
        name, _, value = line.partition("=")
        if name == key:
            spans = _SPAN.findall(value)
            if not spans or _SPAN.sub("", value).strip():
                raise ValueError(f"{RENDER_TIMER.name}: {key} is not a span this reads: {value!r}")
            return sum((int(count) * _UNITS[unit] for count, unit in spans), timedelta())
    raise ValueError(f"{RENDER_TIMER.name} declares no {key}")


RENDER_EVERY = _timer_span("OnUnitActiveSec")
RENDER_DELAY = _timer_span("RandomizedDelaySec")
FULL_READ_EVERY = timedelta(hours=24)
# The installed files are stale once nothing has confirmed them current for
# three runs at their latest: one missed run is a restart or a held lock, two
# is a slow Connect.
CONFIRMED_WITHIN = missed(RENDER_EVERY + RENDER_DELAY, 3).stale_after
# A full read is overdue once the daily one and that same allowance have both
# passed. Never sooner than ``CONFIRMED_WITHIN`` after a
# full read falls due, so a renderer two runs behind raises one claim, not two.
RENDERED_WITHIN = FULL_READ_EVERY + CONFIRMED_WITHIN

# The unit that runs each renderer the launcher names
# (Renderers in controller/host/layout.go), for the exact command. A renderer with no unit
# here gets its rule's steps in words.
RENDERER_UNITS = {"hq": "severino-hq-secrets.service"}

# A failure filed under a word the table below does not hold, or under none.
UNKNOWN_FAILURE = "an error HQ does not recognize"

# What each word a failed run is filed under means (``Class`` in
# controller/secrets/render.go, and the table in docs/Secrets.md).
FAILURE_CLASSES = {
    "config": "the job's configuration was refused",
    "host": "a folder, mount, lock file or destination on the machine is not as it should be",
    "busy": "another refresh was already running",
    "connect_unavailable": "1Password Connect did not answer in time, or the vault kept changing",
    "connect_denied": "1Password Connect refused the token",
    "connect_response": "1Password Connect gave an answer HQ could not use",
    "content": "something in the vault was refused",
    "web_unhealthy": "the web container did not come back after its settings changed",
    "internal": UNKNOWN_FAILURE,
}

# Why the reading holds no document for a renderer, as evidence says it.
UNREAD_REASONS = {
    "missing": "No report found",
    "unreadable": "Report could not be opened",
    "oversized": "Report is too large",
    "invalid": "Report is from a different version",
    "unread": "HQ could not read it",
    "refused": "Report could not be understood",
}

SYNC_SERVICE = "sync"
SYNC_ACTIVE = "ACTIVE"
SYNC_UNREPORTED = "not reported"


@dataclass(frozen=True, slots=True)
class Rendering(ReadingRow):
    """One renderer as its record says it: words and instants. Neither a
    rule's name nor its title says "secret": the findings are held to
    carrying no such word, and what is stale here is said as credentials."""

    KIND = RENDER_STATUS_KIND

    renderer: str = ""
    state: str = RENDER_UNREADABLE
    outcome: str = ""
    failure: str = ""
    attempted_at: str = ""
    confirmed_at: str = ""
    rendered_at: str = ""
    # Connect's sync dependency as it reported it, or "" when the last run
    # never reached Connect.
    sync: str = ""
    sync_read_at: str = ""
    # When the controller read the document.
    read_at: str = ""

    @classmethod
    def record(cls, record: dict[str, Any], snapshot: Any) -> Rendering:
        return rendering(record, snapshot.observed_at)

    @property
    def name(self) -> str:
        return self.renderer or "unnamed"

    @property
    def failed(self) -> bool:
        return self.state == RENDER_READ and self.outcome not in ("rendered", "current")

    @property
    def unconfirmed(self) -> bool:
        """Whether nothing had confirmed the files current within ``CONFIRMED_WITHIN``."""

        return self._behind(self.confirmed_at, CONFIRMED_WITHIN)

    @property
    def unrendered(self) -> bool:
        """Whether no full read had happened within ``RENDERED_WITHIN``."""

        return self._behind(self.rendered_at, RENDERED_WITHIN)

    def _behind(self, stamp: str, within: timedelta) -> bool:
        """Whether ``stamp`` was more than ``within`` old when the document was
        read. A stamp that names no instant is: a renderer that has never
        succeeded has nothing current.
        """

        # Judged as of the reading, never now: while HQ is not in use the
        # controller reads hours apart, and a renderer that has run every hour
        # since is not behind for that. A reading too old to judge is
        # ``render-status-unread``'s (``reading_facts.unjudged``).
        when, read = moment(stamp), moment(self.read_at)
        if when is None:
            return True
        return read is not None and read - when > within


def rendering(record: dict[str, Any], read_at: datetime | None = None) -> Rendering:
    """One `host.render_status` record as the rules read it."""

    status = record.get("status") or {}
    attempt = status.get("last_attempt") or {}
    success = status.get("last_success") or {}
    connect = status.get("connect")
    sync = ""
    if connect is not None:
        sync = next(
            (
                str(item.get("status", ""))
                for item in connect.get("dependencies") or ()
                if item.get("service") == SYNC_SERVICE
            ),
            SYNC_UNREPORTED,
        )
    return Rendering(
        renderer=str(record.get("renderer", "")),
        state=str(record.get("state", "")),
        reason=str(record.get("reason", "")),
        outcome=str(attempt.get("outcome", "")),
        failure=str(attempt.get("failure", "")),
        attempted_at=str(attempt.get("at", "")),
        confirmed_at=str(success.get("at", "")),
        rendered_at=str(success.get("rendered_at", "")),
        sync=sync,
        sync_read_at=str((connect or {}).get("read_at", "")),
        read_at=iso(read_at) or "",
    )


def _stated(estate: FindingEstate):
    return stated(estate, Rendering)


def failed_units(estate: FindingEstate) -> dict[str, frozenset[str]]:
    """The units on each node whose renderer says its last run failed, by node id.

    ``render-failing`` says that failure with its class, so a rule that reads
    unit state leaves these to it, unless the reading is too old for it to.
    """

    return {
        node.id: frozenset(
            unit for rendering in renderings if rendering.failed and (unit := RENDERER_UNITS.get(rendering.renderer))
        )
        for node, renderings in _stated(estate)
    }


def _job(rendering: Rendering, among: tuple[Rendering, ...]) -> str:
    """The words that tell one refresh job from another, when there are several."""

    return f" ({rendering.name})" if len(among) > 1 else ""


def _run_again(
    node: TopologyNode,
    renderings: tuple[Rendering, ...],
    label: str,
    notes: tuple[str, ...] = (),
) -> tuple[Run, ...]:
    """Start each refresh job whose unit HQ knows, on the machine."""

    return tuple(
        step
        for rendering in renderings
        if (unit := RENDERER_UNITS.get(rendering.renderer))
        for step in machine_step(label + _job(rendering, renderings), node.label, f"systemctl start {unit}", notes)
    )


def _journal(node: TopologyNode, renderings: tuple[Rendering, ...]) -> tuple[Run, ...]:
    return tuple(
        step
        for rendering in renderings
        if (unit := RENDERER_UNITS.get(rendering.renderer))
        for step in journal_step(
            "See why" + _job(rendering, renderings),
            node.label,
            unit,
            ("Look for the line with event=secrets.render.failed. The lines above it say what was refused.",),
        )
    )


def _why(rendering: Rendering) -> str:
    """Why a run failed, in words. A word the table does not hold is said
    beside them, so a failure nothing explains is still named."""

    known = FAILURE_CLASSES.get(rendering.failure)
    if known or not rendering.failure:
        return known or UNKNOWN_FAILURE
    return f"{UNKNOWN_FAILURE} ({rendering.failure})"


def _failing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A renderer whose last run failed.

    The files it installed before stay in place, so everything keeps working
    on them. Serious once they are also past ``CONFIRMED_WITHIN``: a failure
    that has outlasted the allowance is no longer one bad run.
    """

    found = []
    for node, renderings in _stated(estate):
        failed = tuple(rendering for rendering in renderings if rendering.failed)
        if not failed:
            continue
        stale = any(rendering.unconfirmed for rendering in failed)
        reasons = ", ".join(dict.fromkeys(_why(rendering) for rendering in failed))
        last_good = moment(failed[0].confirmed_at) if len(failed) == 1 else None
        found.append(
            Finding(
                rule="render-failing",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=f"{node.label} could not refresh its credentials: {reasons}",
                severity=Tone.SERIOUS if stale else Tone.ATTENTION,
                explanation=(
                    f"Changes in 1Password since {when(failed[0].confirmed_at, estate.now)} have not reached it."
                    if last_good is not None
                    else "Changes in 1Password have not reached it."
                ),
                evidence=tuple(
                    item
                    for rendering in failed
                    for item in (
                        ("Job", rendering.name),
                        ("Reason", _why(rendering)),
                        ("Last tried", when(rendering.attempted_at, estate.now)),
                        ("Last refreshed", when(rendering.confirmed_at, estate.now)),
                    )
                ),
                steps=_journal(node, failed) + _run_again(node, failed, "Run it again"),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _stale_evidence(rendering: Rendering, now: datetime) -> tuple[tuple[str, str], ...]:
    return (
        ("Job", rendering.name),
        ("Last refreshed", when(rendering.confirmed_at, now)),
        ("Last read in full", when(rendering.rendered_at, now)),
        ("Last tried", when(rendering.attempted_at, now)),
    )


def _stale_title(machine: str, stale: tuple[Rendering, ...]) -> str:
    """What has not happened, and since when: the refresh, or only the full read."""

    first = stale[0]
    stamp, did = (
        (first.confirmed_at, "refreshed its credentials")
        if first.unconfirmed
        else (first.rendered_at, "read its credentials in full")
    )
    since = moment(stamp)
    if since is None:
        return f"{machine} has never {did}"
    return f"{machine} has not {did} {since_when(since)}"


def _stale(estate: FindingEstate) -> tuple[Finding, ...]:
    """A renderer that has stopped running, or stopped reading in full.

    Its last run did not fail and nothing has confirmed the installed files
    since. A stopped reading is ``render-status-unread``'s to say, and a failed
    run ``render-failing``'s.
    """

    found = []
    for node, renderings in _stated(estate):
        stale = tuple(
            rendering
            for rendering in renderings
            if rendering.state == RENDER_READ
            and not rendering.failed
            and (rendering.unconfirmed or rendering.unrendered)
        )
        if not stale:
            continue
        found.append(
            Finding(
                rule="render-stale",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=_stale_title(node.label, stale),
                severity=Tone.SERIOUS,
                explanation="Its refresh job is not running.",
                evidence=(
                    *(item for rendering in stale for item in _stale_evidence(rendering, estate.now)),
                    ("Due", f"Every {duration(RENDER_EVERY)}, in full every {duration(FULL_READ_EVERY)}"),
                ),
                steps=tuple(
                    step
                    for rendering in stale
                    if (unit := RENDERER_UNITS.get(rendering.renderer))
                    for step in machine_step(
                        "See when it last ran and is next due" + _job(rendering, stale),
                        node.label,
                        f"systemctl list-timers {unit.removesuffix('.service')}.timer",
                    )
                )
                + _journal(node, stale)
                + _run_again(node, stale, "Run it now"),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _sync_stalled(estate: FindingEstate) -> tuple[Finding, ...]:
    """A Connect server whose synchronization with 1Password is not active.

    A cached read still succeeds, so every render reports success while the
    vault it reads has stopped following the real one.
    """

    found = []
    for node, renderings in _stated(estate):
        stalled = tuple(
            rendering
            for rendering in renderings
            if rendering.state == RENDER_READ and rendering.sync and rendering.sync != SYNC_ACTIVE
        )
        if not stalled:
            continue
        found.append(
            Finding(
                rule="connect-sync-stalled",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=f"1Password Connect on {node.label} has stopped syncing",
                severity=Tone.ATTENTION,
                explanation=ended(f"Changes in 1Password are not reaching {node.label}"),
                evidence=tuple(
                    item
                    for rendering in stalled
                    for item in (
                        ("Job", rendering.name),
                        ("Updates from 1Password", rendering.sync),
                        ("Connect last read", when(rendering.sync_read_at, estate.now)),
                    )
                ),
                steps=(
                    Do(
                        f"Restart 1Password Connect on {node.label} and read its sync container's log",
                        notes=("TOKEN_NEEDED means it has not been given its credentials file.",),
                    ),
                    *_run_again(node, stalled, "Then run the refresh again"),
                ),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _unread(estate: FindingEstate) -> tuple[Finding, ...]:
    """A renderer whose status document is missing or could not be read, or
    whose reading is too old to say how it stands now.

    HQ cannot say whether its secrets are fresh, which is not the same as
    saying they are.
    """

    found = [
        aged.finding("render-status-unread", RENDER_STATUS_KIND, _cannot_tell(aged.node))
        for aged in unjudged(estate, Rendering)
    ]
    for node, renderings in _stated(estate):
        unread = tuple(rendering for rendering in renderings if rendering.state != RENDER_READ)
        if not unread:
            continue
        found.append(
            Finding(
                rule="render-status-unread",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=_cannot_tell(node),
                severity=Tone.ATTENTION,
                explanation="Its refresh job has not reported.",
                evidence=tuple(
                    item
                    for rendering in unread
                    for item in (
                        ("Job", rendering.name),
                        (
                            "Report",
                            UNREAD_REASONS.get(rendering.reason, "Could not be read"),
                        ),
                    )
                ),
                steps=_run_again(node, unread, "Run it once", ("If this stays, deploy HQ.",)),
                no_help_reason=cannot_run_commands(node.label),
            )
        )
    return tuple(found)


def _cannot_tell(node: TopologyNode) -> str:
    return f"HQ cannot tell whether {node.label}'s credentials are fresh"


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "render-failing",
        "Credentials could not be refreshed",
        Tone.SERIOUS,
        _failing,
        gathered=True,
        operator_action="Read the refresh job's log, fix the cause, then run it again.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: f"a refresh fails and nothing has confirmed credentials current for {duration(CONFIRMED_WITHIN)}",
            Tone.ATTENTION: "the last refresh of credentials failed",
        },
        clears="a refresh of credentials succeeds",
    ),
    FindingRule(
        "render-stale",
        "Credentials not refreshed lately",
        Tone.SERIOUS,
        _stale,
        gathered=True,
        operator_action="Check the refresh job's timer is on, then run the job.",
        no_help_reason=cannot_run_commands(),
        bars={
            Tone.SERIOUS: (
                f"nothing has confirmed credentials current for {duration(CONFIRMED_WITHIN)}, "
                f"or read 1Password in full for {duration(RENDERED_WITHIN)}"
            )
        },
        clears=f"a refresh confirms credentials current within {duration(CONFIRMED_WITHIN)}",
    ),
    FindingRule(
        "connect-sync-stalled",
        "1Password Connect has stopped syncing",
        Tone.ATTENTION,
        _sync_stalled,
        gathered=True,
        operator_action="Restart 1Password Connect, then run the refresh job.",
        no_help_reason=cannot_run_commands(),
        bars={Tone.ATTENTION: "1Password Connect no longer reports its sync as active"},
    ),
    FindingRule(
        "render-status-unread",
        "Cannot tell whether credentials are fresh",
        Tone.ATTENTION,
        _unread,
        gathered=True,
        operator_action="Run the refresh job. If this stays, deploy HQ.",
        no_help_reason=cannot_run_commands(),
        bars={Tone.ATTENTION: "HQ cannot read when credentials were last refreshed"},
    ),
)
