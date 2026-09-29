"""Whether a running container can be upgraded, and exactly how it would be.

A plan is read-only: it changes nothing, and is what an upgrade would do. It
names the target by digest (what gets pinned, scanned and verified), what the
move is (patch, minor, major), what it fixes and what it would bring, whether
the container holds data that has to be snapshotted first, how the result can
be verified, and every reason it cannot go ahead yet, each with an id an agent
can act on. The same plan is on the container's page and in the ``upgrades``
resource, so a person and an agent read one answer.

HQ's own image is never planned here: it is deployed by its own signed
pipeline, and only by that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .containers import SERIOUS, BEHIND, VULNERABLE, Container, containers, ranked
from .images import affected, version

PATCH = "patch"
MINOR = "minor"
MAJOR = "major"
UNKNOWN_CHANGE = "unknown"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"


@dataclass(frozen=True)
class Blocker:
    id: str
    reason: str


@dataclass(frozen=True)
class Step:
    id: str
    label: str
    detail: str


@dataclass(frozen=True)
class Plan:
    container: Container
    target_tag: str
    target_digest: str
    change: str
    release: Mapping[str, Any] | None
    # Advisories and vulnerabilities against what runs that the target is clear
    # of, and any serious one the target would bring.
    fixes: tuple[Mapping[str, Any], ...]
    introduces: tuple[Mapping[str, Any], ...]
    # Writable mounts holding its data: snapshotted before, restored on rollback.
    data: tuple[Mapping[str, Any], ...]
    verified_by: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    # Why it would not apply on its own, though it may by a person's approval.
    not_automatic: tuple[Blocker, ...]
    steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def viable(self) -> bool:
        return not self.blockers

    @property
    def stateful(self) -> bool:
        return bool(self.data)

    @property
    def risk(self) -> str:
        if self.change == MAJOR or (self.stateful and not self.verified_by) or self.introduces:
            return HIGH
        if self.change == PATCH and self.verified_by and not self.stateful:
            return LOW
        return MEDIUM


def change_between(running: str, target: str) -> str:
    """The size of the move, by the first number that differs."""

    old, new = version(running), version(target)
    if not old or not new:
        return UNKNOWN_CHANGE
    width = max(len(old), len(new))
    old, new = old + (0,) * (width - len(old)), new + (0,) * (width - len(new))
    for index, (was, becomes) in enumerate(zip(old, new)):
        if was != becomes:
            return (MAJOR, MINOR)[index] if index < 2 else PATCH
    return UNKNOWN_CHANGE


def data_of(mounts) -> tuple[Mapping[str, Any], ...]:
    """The writable mounts that hold what the container keeps."""

    from .container_standard import DOCKER_SOCKETS, runtime_path, system_path

    return tuple(
        mount
        for mount in mounts or ()
        if not mount.get("read_only")
        and mount.get("type") in ("volume", "bind")
        and mount.get("source") not in DOCKER_SOCKETS
        # A socket is a way to talk to something, not something kept.
        and not str(mount.get("source", "")).endswith(".sock")
        and not (mount.get("type") == "bind" and system_path(str(mount.get("source", ""))))
        # A runtime directory is emptied by every boot: a doorbell or a lock
        # file there is state for this run, never data to keep.
        and not (mount.get("type") == "bind" and runtime_path(str(mount.get("source", ""))))
    )


def plan_for(item: Container) -> Plan | None:
    """The plan for one container, or None when nothing newer is published."""

    standing = item.standing
    if standing.state not in (BEHIND, VULNERABLE) or not standing.newer:
        return None
    target = standing.latest
    fixes = tuple(
        advisory
        for advisory in standing.advisories
        if affected(target, [tuple(pair) for pair in advisory.get("vulnerabilities") or ()]) is False
    ) + _cleared(standing)
    introduces = tuple(
        advisory
        for advisory in standing.advisories
        if affected(target, [tuple(pair) for pair in advisory.get("vulnerabilities") or ()]) is True
    ) + _brought(standing)
    data = data_of(item.mounts)
    verified_by = _verified_by(item)
    change = change_between(standing.tag, target)
    blockers = _blockers(item, standing, introduces)
    return Plan(
        container=item,
        target_tag=target,
        target_digest=standing.target_digest,
        change=change,
        release=standing.release,
        fixes=fixes,
        introduces=introduces,
        data=data,
        verified_by=verified_by,
        blockers=blockers,
        not_automatic=_not_automatic(change, verified_by, blockers, _unvetted(standing)),
        steps=_steps(item, target, standing.target_digest, data, verified_by),
    )


def _found(checked: Mapping[str, Any] | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(str(item.get("id")), str(item.get("package"))): item for item in (checked or {}).get("findings") or ()}


def _cleared(standing: Any) -> tuple[Mapping[str, Any], ...]:
    """Vulnerabilities in what runs that the target's packages are clear of,
    when both digests' packages were checked."""

    if standing.checked is None or standing.target_checked is None:
        return ()
    after = _found(standing.target_checked)
    return ranked(item for key, item in _found(standing.checked).items() if key not in after)


def _brought(standing: Any) -> tuple[Mapping[str, Any], ...]:
    """Serious vulnerabilities in the target that what runs does not have."""

    if standing.checked is None or standing.target_checked is None:
        return ()
    before = _found(standing.checked)
    return ranked(
        item
        for key, item in _found(standing.target_checked).items()
        if key not in before and item.get("severity") in SERIOUS
    )


def _unvetted(standing: Any) -> tuple[Blocker, ...]:
    """What is not yet known about the target: its build, and its packages."""

    attested = standing.target_attested
    if attested is None or attested.get("unread"):
        return (Blocker("target-unread", "What the target's publisher attached to it has not been read yet."),)
    found = []
    if not attested.get("provenance"):
        found.append(Blocker("no-provenance", "The target's build is not described, so what it was built from is unknown."))
    if not attested.get("packages"):
        found.append(Blocker("no-package-list", "The target lists no packages, so nothing can check them."))
    elif standing.target_checked is None:
        found.append(Blocker("not-scanned", "The target's packages have not been checked yet."))
    return tuple(found)


def _verified_by(item: Container) -> tuple[str, ...]:
    """What would say a new version works: its own health check, as its
    inspect or its status reports one, and a request to each name it serves."""

    checked = bool((item.runtime or {}).get("healthcheck")) or bool(item.running.check)
    return tuple(
        what
        for what, present in (
            ("its health check", checked),
            *((f"a request to {hostname}", True) for hostname in item.serves),
        )
        if present
    )


def _blockers(item: Container, standing: Any, introduces) -> tuple[Blocker, ...]:
    found = []
    if standing.repository is not None:
        found.append(Blocker("own-pipeline", "HQ's own image is deployed by its signed pipeline, never by an upgrade."))
    if not standing.target_digest:
        found.append(Blocker("no-target-digest", f"The digest {standing.latest} names has not been read yet."))
    if not item.running.watcher:
        found.append(Blocker("not-declared", "HQ does not watch this container. Adopt it first."))
    if item.mounts is None:
        found.append(Blocker("mounts-unread", "Its mounts have not been read, so its data cannot be found to snapshot."))
    if introduces:
        found.append(Blocker("target-affected", f"{standing.latest} brings a known vulnerability of its own."))
    # Until a machine carries the upgrade helper, nothing can apply a plan.
    found.append(Blocker("no-apply-path", f"{item.machine.name} has no upgrade helper installed yet."))
    return tuple(found)


def _not_automatic(change: str, verified_by, blockers, unvetted=()) -> tuple[Blocker, ...]:
    """What keeps it from applying without a person: every blocker, what is
    not known about the target, and then the bar only a vetted patch clears."""

    found = [*blockers, *unvetted]
    if change != PATCH:
        found.append(Blocker("not-a-patch", f"A {change} change waits for a person."))
    if not verified_by:
        found.append(Blocker("unverifiable", "Nothing but its running state could say the new version works."))
    found.append(Blocker("not-opted-in", "This container is not opted in to automatic upgrades."))
    return tuple(found)


def _steps(item: Container, target: str, digest: str, data, verified_by) -> tuple[Step, ...]:
    where = ", ".join(item.compose_files) or f"{item.running.stack or item.running.name} on {item.machine.name}"
    pinned = f"{item.standing.image.short}:{target}, " + ("by its digest" if digest else "once its digest is read")
    steps = []
    if data:
        steps.append(Step("snapshot", "Snapshot its data", ", ".join(str(mount.get("source")) for mount in data)))
    steps += [
        Step("pull", "Pull the target by digest", pinned),
        Step(
            "trial",
            "Run it beside the live one",
            "On an internal network" + (", against a copy of its data" if data else "") + ", until it reports healthy.",
        ),
        Step("pin", "Pin the digest in its compose file", where),
        Step("apply", "Recreate the service", f"compose up for {item.running.name} only"),
        Step("verify", "Verify", ", ".join(verified_by) if verified_by else "That it keeps running."),
        Step(
            "keep-or-roll-back",
            "Keep it, or roll back",
            (
                "The previous digest and the snapshot are restored if verification fails."
                if data
                else "The previous digest is restored if verification fails."
            ),
        ),
    ]
    return tuple(steps)


@dataclass(frozen=True)
class Readiness:
    """What an upgrade of this container would take, before any is published:
    what would verify it, what would be snapshotted, and what keeps it from
    applying on its own. The same answer a plan gives, without a target."""

    container: Container
    data: tuple[Mapping[str, Any], ...]
    verified_by: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    not_automatic: tuple[Blocker, ...]

    @property
    def automatic(self) -> bool:
        return not self.not_automatic


def readiness_of(item: Container) -> Readiness:
    data = data_of(item.mounts)
    verified_by = _verified_by(item)
    blockers = tuple(
        blocker
        for blocker in _blockers(item, item.standing, ())
        if blocker.id not in ("no-target-digest", "target-affected")
    )
    not_automatic = tuple(
        blocker
        for blocker in _not_automatic(PATCH, verified_by, blockers, _uncheckable(item.standing))
        if blocker.id != "not-a-patch"
    )
    return Readiness(item, data, verified_by, blockers, not_automatic)


def _uncheckable(standing: Any) -> tuple[Blocker, ...]:
    """Whether a release of this image could be checked when one lands: its
    publisher lists the packages of what runs now, or it does not."""

    attested = standing.attested
    if attested is None or attested.get("unread"):
        return (Blocker("attestations-unread", "What its publisher attaches to an image has not been read yet."),)
    if not attested.get("packages"):
        return (Blocker("no-package-list", "Its publisher lists no packages, so no release of it can be checked."),)
    return ()


def plans() -> list[Plan]:
    """Every container something newer is published for, least risky first."""

    order = (LOW, MEDIUM, HIGH)
    found = [plan for plan in (plan_for(item) for item in containers()) if plan is not None]
    return sorted(found, key=lambda plan: (not plan.fixes, order.index(plan.risk), plan.container.address))
