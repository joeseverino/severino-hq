"""Findings about the secrets a machine renders: a render that failed, one that
stopped running, a Connect server that stopped syncing, and a status document
nobody could read.

Each secret renderer writes a status document after every run, and the
controller on that machine reports it as the ``host.render_status`` reading
(``docs/SECRETS.md``). Cached files keep everything running while a renderer
fails, so nothing else notices: a rotated credential never arrives and a
revoked one keeps working. The reading is one fact per renderer on the node of
the controller that read it, and the rules here read those facts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any

from hq.domains.control_plane.observations.host import (
    RENDER_READ,
    RENDER_UNREADABLE,
    RENDER_STATUS_KIND,
)

from .derivations import passed, since
from .finding_model import Finding, FindingEstate, FindingRule, OperatorStep, fact_values
from .moments import duration
from .timestamps import moment
from .topology_model import TopologyNode, derived_id
from .ui import counted

# The fact a node carries per renderer its controller reported. Neither it nor
# a rule's name or title says "secret": the findings and the topology are held
# to carrying no such word, and what is stale here is said as credentials.
RENDER_STATUS = "render-status"

# The renderer's timer starts it every hour, up to five minutes late
# (OnUnitActiveSec and RandomizedDelaySec in
# deploy/systemd/severino-hq-secrets.timer), and a run that finds the vault
# unchanged still reads it in full once a day (FullEvery in
# controller/cmd/hq-secrets/main.go). ``test_render_status`` holds these three
# to those files.
RENDER_EVERY = timedelta(hours=1)
RENDER_DELAY = timedelta(minutes=5)
FULL_READ_EVERY = timedelta(hours=24)
# How many runs may pass without a success before the secrets are stale. Three,
# as for a kind no sweep reaches: one missed run is a restart or a held lock,
# two is a slow Connect.
MISSED_RUNS = 3
# The installed files are stale once nothing has confirmed them current for
# this long: 3 hours 15 minutes, three runs at their latest.
CONFIRMED_WITHIN = MISSED_RUNS * (RENDER_EVERY + RENDER_DELAY)
# A full read is overdue once the daily one and that same allowance have both
# passed: 27 hours 15 minutes. Never sooner than ``CONFIRMED_WITHIN`` after a
# full read falls due, so a renderer two runs behind raises one claim, not two.
RENDERED_WITHIN = FULL_READ_EVERY + CONFIRMED_WITHIN

# The unit that runs each renderer the launcher names
# (scripts/run-controller.sh), for the exact command. A renderer with no unit
# here gets its rule's steps in words.
RENDERER_UNITS = {"hq": "severino-hq-secrets.service"}

# What each word a failed run is filed under means (``Class`` in
# controller/secrets/render.go, and the table in docs/SECRETS.md).
FAILURE_CLASSES = {
    "config": "the unit's configuration or the connection registry was refused",
    "host": "a directory, mount, lock file or destination was not as required",
    "busy": "another render held the lock",
    "connect_unavailable": "Connect gave no answer within the deadline, or the vault kept changing",
    "connect_denied": "Connect refused the reader token",
    "connect_response": "Connect's answer was malformed, oversized, redirected or for another vault",
    "content": "what the vault holds was refused",
    "web_unhealthy": "the web container did not come back after its environment changed",
    "internal": "the renderer failed in a way it has no word for",
}

# Why the reading holds no document for a renderer, as evidence says it.
UNREAD_REASONS = {
    "missing": "no status document",
    "unreadable": "the status document could not be opened",
    "oversized": "the status document is larger than one can be",
    "invalid": "the status document is not the shape this release reads",
    "unread": "the controller could not take the reading",
    "refused": "the record did not match the reading's schema",
}

SYNC_SERVICE = "sync"
SYNC_ACTIVE = "ACTIVE"
SYNC_UNREPORTED = "not reported"


@dataclass(frozen=True)
class Rendering:
    """One renderer as its fact carries it: words and instants, in field order."""

    renderer: str = ""
    state: str = RENDER_UNREADABLE
    reason: str = ""
    outcome: str = ""
    failure: str = ""
    attempted_at: str = ""
    confirmed_at: str = ""
    rendered_at: str = ""
    # Connect's sync dependency as it reported it, or "" when the last run
    # never reached Connect.
    sync: str = ""
    sync_read_at: str = ""

    @property
    def fact(self) -> tuple[str, str]:
        return RENDER_STATUS, "|".join(getattr(self, field.name) for field in fields(self))

    @classmethod
    def of(cls, value: str) -> Rendering:
        names = [field.name for field in fields(cls)]
        parts = (value.split("|") + [""] * len(names))[: len(names)]
        return cls(**dict(zip(names, parts)))

    @property
    def name(self) -> str:
        return self.renderer or "a renderer"

    @property
    def failed(self) -> bool:
        return self.state == RENDER_READ and self.outcome not in ("rendered", "current")

    def unconfirmed(self, now: datetime) -> bool:
        """Whether nothing has confirmed the files current within ``CONFIRMED_WITHIN``."""

        return _overdue(self.confirmed_at, CONFIRMED_WITHIN, now)

    def unrendered(self, now: datetime) -> bool:
        """Whether no full read has happened within ``RENDERED_WITHIN``."""

        return _overdue(self.rendered_at, RENDERED_WITHIN, now)


def _overdue(stamp: str, within: timedelta, now: datetime) -> bool:
    """Whether ``stamp`` is more than ``within`` old. A stamp that names no
    instant is: a renderer that has never succeeded has nothing current.

    The one threshold against the clock in this module, asked of the estate's
    own ``now`` through ``passed``, so the answer says when it stops holding.
    """

    when = moment(stamp)
    return when is None or passed(when + within, now=now)


def _rendering(record: dict[str, Any]) -> Rendering:
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
    )


def _renderings(snapshot: Any) -> Iterator[Rendering]:
    """What one stored reading says: a record per renderer, and the reading's
    own failure as a renderer nobody could name."""

    if not snapshot.reachable:
        yield Rendering(reason="unread")
        return
    for record in snapshot.records or ():
        yield _rendering(record)
    if snapshot.error:
        yield Rendering(reason="refused")


def add(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> None:
    """One fact per renderer, on the node of the controller that read it.

    ``machine`` names the machine node a controller folded into. A reading
    whose controller no node stands for gets a node of its own, so a renderer
    that went quiet is never dropped for want of somewhere to say it.
    """

    from .facts import snapshots_of

    for snapshot in snapshots_of(RENDER_STATUS_KIND):
        found = tuple(rendering.fact for rendering in _renderings(snapshot))
        if not found:
            continue
        controller = str(getattr(snapshot, "controller_id", "") or "")
        node_id = derived_id("controller", controller)
        if node_id not in nodes:
            node_id = machine(controller) or node_id
        node = nodes.get(node_id) or TopologyNode(
            id=node_id, kind="controller", label=controller or "The controller", subtitle="Controller"
        )
        nodes[node_id] = replace(node, facts=node.facts + found)


def _stated(estate: FindingEstate) -> Iterator[tuple[TopologyNode, tuple[Rendering, ...]]]:
    for node in estate.nodes():
        values = fact_values(node, RENDER_STATUS)
        if values:
            yield node, tuple(Rendering.of(value) for value in values)


def _names(renderings: tuple[Rendering, ...]) -> str:
    return ", ".join(rendering.name for rendering in renderings)


def _when(stamp: str, now: datetime) -> str:
    """An instant as an age, in words that hold until the age reads differently."""

    when = moment(stamp)
    return f"{duration(since(when, now=now))} ago" if when is not None else "never"


def _run_again(renderings: tuple[Rendering, ...], label: str) -> tuple[OperatorStep, ...]:
    """Start each renderer whose unit HQ knows, on the machine."""

    return tuple(
        OperatorStep(label=label.format(name=rendering.name), command=f"sudo systemctl start {unit}")
        for rendering in renderings
        if (unit := RENDERER_UNITS.get(rendering.renderer))
    )


def _journal(renderings: tuple[Rendering, ...]) -> tuple[OperatorStep, ...]:
    return tuple(
        OperatorStep(
            label=f"Read why the {rendering.name} renderer's last run ended, on the machine",
            command=f"sudo journalctl -u {unit} -n 50 --no-pager",
            notes=(
                "The line with event=secrets.render.failed names the class; "
                "the lines above it say what was refused.",
            ),
        )
        for rendering in renderings
        if (unit := RENDERER_UNITS.get(rendering.renderer))
    )


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
        stale = any(rendering.unconfirmed(estate.now) for rendering in failed)
        classes = ", ".join(dict.fromkeys(rendering.failure or "unknown" for rendering in failed))
        found.append(
            Finding(
                rule="render-failing",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=f"Credentials on {node.label} failed to render ({classes})",
                severity="serious" if stale else "attention",
                explanation=(
                    "The last run of the credential renderer failed, so the machine is "
                    "running on the files an earlier run installed. A credential "
                    "rotated or revoked in the vault since then has not reached it. "
                    "Fix what the failure names and run the renderer again."
                ),
                evidence=tuple(
                    item
                    for rendering in failed
                    for item in (
                        ("Renderer", rendering.name),
                        (
                            "Failure",
                            f"{rendering.failure or 'unknown'}: "
                            + FAILURE_CLASSES.get(rendering.failure, "a class this release does not know"),
                        ),
                        ("Last attempt", _when(rendering.attempted_at, estate.now)),
                        ("Last success", _when(rendering.confirmed_at, estate.now)),
                    )
                ),
                steps=_journal(failed) + _run_again(failed, "Run the {name} renderer again once that is fixed"),
            )
        )
    return tuple(found)


def _stale_evidence(rendering: Rendering, now: datetime) -> tuple[tuple[str, str], ...]:
    return (
        ("Renderer", rendering.name),
        ("Last confirmed current", _when(rendering.confirmed_at, now)),
        ("Last read in full", _when(rendering.rendered_at, now)),
        ("Last attempt", _when(rendering.attempted_at, now)),
    )


def _stale(estate: FindingEstate) -> tuple[Finding, ...]:
    """A renderer that has stopped running, or stopped reading in full.

    The last run it recorded did not fail, and nothing has confirmed the
    installed files since: the timer is not firing, the unit does not start, or
    it fails before taking the lock, which records nothing. Measured
    against now and not against when the reading was taken, so a controller
    that stopped sweeping because its secrets are gone still raises this.
    A failed run is ``render-failing``'s to say.
    """

    found = []
    for node, renderings in _stated(estate):
        stale = tuple(
            rendering
            for rendering in renderings
            if rendering.state == RENDER_READ
            and not rendering.failed
            and (rendering.unconfirmed(estate.now) or rendering.unrendered(estate.now))
        )
        if not stale:
            continue
        found.append(
            Finding(
                rule="render-stale",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=f"Credentials on {node.label} have not been refreshed ({_names(stale)})",
                severity="serious",
                explanation=(
                    f"The renderer runs every {duration(RENDER_EVERY)} and reads the vault "
                    f"in full every {duration(FULL_READ_EVERY)}. Nothing has confirmed the "
                    f"installed files within {duration(CONFIRMED_WITHIN)}, or read them in "
                    f"full within {duration(RENDERED_WITHIN)}. Either the renderer is not "
                    "running, or it fails before it can record a run: a refused "
                    "configuration, a failed host check and a held lock write no status. "
                    "Check its timer and its journal, then start it."
                ),
                evidence=tuple(
                    item for rendering in stale for item in _stale_evidence(rendering, estate.now)
                ),
                steps=tuple(
                    OperatorStep(
                        label=f"See when the {rendering.name} renderer's timer last fired and is next due, on the machine",
                        command=f"systemctl list-timers {unit.removesuffix('.service')}.timer",
                    )
                    for rendering in stale
                    if (unit := RENDERER_UNITS.get(rendering.renderer))
                )
                + _journal(stale)
                + _run_again(stale, "Run the {name} renderer now"),
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
        states = ", ".join(dict.fromkeys(rendering.sync for rendering in stalled))
        found.append(
            Finding(
                rule="connect-sync-stalled",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=f"1Password Connect on {node.label} is not syncing ({states})",
                severity="attention",
                explanation=(
                    "Connect reports its synchronization with 1Password as not active. "
                    "It keeps answering from its cache, so renders succeed on a vault "
                    "that has stopped changing: a rotation does not arrive and a "
                    "revocation does not take effect. Restore Connect's sync, then "
                    "run the renderer."
                ),
                evidence=tuple(
                    item
                    for rendering in stalled
                    for item in (
                        ("Renderer", rendering.name),
                        ("Sync", rendering.sync),
                        ("Connect read", _when(rendering.sync_read_at, estate.now)),
                    )
                ),
                steps=(
                    OperatorStep(
                        label="Restart the Connect server on the machine and read its sync "
                        "container's log; TOKEN_NEEDED means it has not been given its "
                        "credentials file."
                    ),
                    *_run_again(stalled, "Run the {name} renderer so its status is read again"),
                ),
            )
        )
    return tuple(found)


def _unread(estate: FindingEstate) -> tuple[Finding, ...]:
    """A renderer whose status document is missing or could not be read.

    HQ cannot say whether its secrets are fresh, which is not the same as
    saying they are.
    """

    found = []
    for node, renderings in _stated(estate):
        unread = tuple(rendering for rendering in renderings if rendering.state != RENDER_READ)
        if not unread:
            continue
        found.append(
            Finding(
                rule="render-status-unread",
                subject=node.id,
                scope=RENDER_STATUS_KIND,
                title=(
                    f"{node.label} has no readable render status "
                    f"({counted(len(unread), 'renderer')})"
                ),
                severity="attention",
                explanation=(
                    "The renderer writes a status document after every run, and the "
                    "controller on the machine could not read one. Either the renderer "
                    "has not run since the machine started, or the document is not the "
                    "one this release reads. Run the renderer, and deploy if the "
                    "document is still refused."
                ),
                evidence=tuple(
                    item
                    for rendering in unread
                    for item in (
                        ("Renderer", rendering.name),
                        (
                            "Status document",
                            UNREAD_REASONS.get(rendering.reason, "could not be read"),
                        ),
                    )
                ),
                steps=_run_again(unread, "Run the {name} renderer, which writes its status"),
            )
        )
    return tuple(found)


_NO_SHELL = (
    "The renderer runs as root on the machine, outside any container, and HQ holds "
    "no shell there and no credential that can start a unit."
)

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "render-failing",
        "Credential render failing",
        "serious",
        _failing,
        operator_action=(
            "Read the renderer unit's journal on the machine for the "
            "event=secrets.render.failed line, fix what its class names, then start the unit."
        ),
        no_help_reason=_NO_SHELL,
    ),
    FindingRule(
        "render-stale",
        "Credentials not refreshed",
        "serious",
        _stale,
        operator_action=(
            "Check that the renderer's timer is enabled and firing on the machine, "
            "then start the renderer's unit."
        ),
        no_help_reason=_NO_SHELL,
    ),
    FindingRule(
        "connect-sync-stalled",
        "Connect not syncing",
        "attention",
        _sync_stalled,
        operator_action=(
            "Restore the Connect server's synchronization on the machine "
            "(its sync container and credentials file), then start the renderer's unit."
        ),
        no_help_reason=(
            "Connect is a server on the machine that only root's renderer may reach, "
            "and HQ holds no credential for it."
        ),
    ),
    FindingRule(
        "render-status-unread",
        "Render status not read",
        "attention",
        _unread,
        operator_action=(
            "Start the renderer's unit on the machine so it writes its status document, "
            "and deploy if the controller still refuses the document."
        ),
        no_help_reason=_NO_SHELL,
    ),
)
