"""Whether a running container can be upgraded, and exactly how it would be.

A plan changes nothing; what it holds is docs/Derived-Facts.md's to list. The
same plan is on the container's page and in the ``upgrades`` resource, so a
person and an agent read one answer, each blocker an id an agent can act on.
HQ's own image is never upgraded from here: only its signed pipeline deploys it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from django.utils.text import capfirst, get_text_list

from .card_items import Answer
from .containers import BEHIND, SERIOUS, VULNERABLE, Container, containers, ranked
from .describe import Describe
from .display_words import Code
from .entity_links import EntityLink, declared_link
from .images import affected, version
from .tone import Tone
from .ui import counted, ended
lazy from . import exposure
lazy from .container_standard import DOCKER_SOCKETS, runtime_path, system_path
lazy from .exposure import LEVELS

PATCH = "patch"
MINOR = "minor"
MAJOR = "major"
UNKNOWN_CHANGE = "unknown"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"


@dataclass(frozen=True, slots=True)
class Blocker:
    id: Annotated[str, Describe("What stands in the way, as a stable id an agent can act on, such as `mounts-unread`.")]
    reason: Annotated[str, Describe("What stands in the way, in a sentence.")]


@dataclass(frozen=True, slots=True)
class Plan:
    container: Container
    target_tag: str
    target_digest: str
    change: str
    release: Mapping[str, Any] | None
    # Of what makes the running version vulnerable, what the target is clear
    # of, and what known vulnerability the target would bring of its own.
    fixes: tuple[Mapping[str, Any], ...]
    introduces: tuple[Mapping[str, Any], ...]
    # Writable mounts holding the data it keeps.
    data: tuple[Mapping[str, Any], ...]
    verified_by: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    # Why it would not apply on its own, though it may by a person's approval.
    not_automatic: tuple[Blocker, ...]

    @property
    def viable(self) -> bool:
        return not self.blockers

    @property
    def recommended(self) -> bool:
        """Whether moving to the target is worth doing: it brings no known
        vulnerability of its own and, where the running version has some,
        fixes at least one. Everything that offers the move reads this."""

        return not self.introduces and (bool(self.fixes) or not self.container.standing.known_vulnerabilities)

    @property
    def stateful(self) -> bool:
        return bool(self.data)

    @property
    def fixes_said(self) -> Answer:
        standing = self.container.standing
        if self.fixes:
            return fixes_said(self.fixes)
        if standing.known_vulnerabilities:
            return Answer(f"None of {len(standing.known_vulnerabilities)}")
        return Answer(capfirst(standing.vulnerabilities), (standing.vulnerabilities_note,))

    @property
    def data_said(self) -> Answer:
        return data_said(self.data, Tone.ATTENTION)

    @property
    def verified_said(self) -> Answer:
        return verified_said(self.verified_by)

    @property
    def risk(self) -> str:
        if self.change == MAJOR or (self.stateful and not self.verified_by) or self.introduces:
            return HIGH
        if self.change == PATCH and self.verified_by and not self.stateful:
            return LOW
        return MEDIUM

    @property
    def change_label(self) -> str:
        """The size of the move, as a page says it: "Patch update"."""

        return f"{self.change.capitalize()} update" if self.change != UNKNOWN_CHANGE else "Update"

    @property
    def risk_reason(self) -> str:
        """Why upgrading carries the risk it does, or "" when the risk is low.

        The risk is of the upgrade going wrong, never the severity of what it
        fixes."""

        reasons = []
        if self.change in (MAJOR, MINOR):
            reasons.append(f"a {self.change} version")
        if self.introduces:
            reasons.append("a vulnerable new version")
        if self.stateful:
            reasons.append("stored data")
        if not self.verified_by:
            reasons.append("no check afterwards")
        return "" if self.risk == LOW else get_text_list(reasons, "and")

    @property
    def risk_said(self) -> str:
        """The risk as a sentence, as every page and card says it."""

        reason = self.risk_reason
        return ended(f"{capfirst(self.risk)} risk: {reason}" if reason else f"{capfirst(self.risk)} risk")


def change_between(running: str, target: str) -> str:
    """The size of the move, by the first number that differs."""

    old, new = version(running), version(target)
    if not old or not new:
        return UNKNOWN_CHANGE
    width = max(len(old), len(new))
    old, new = old + (0,) * (width - len(old)), new + (0,) * (width - len(new))
    for index, (was, becomes) in enumerate(zip(old, new, strict=True)):
        if was != becomes:
            return (MAJOR, MINOR)[index] if index < 2 else PATCH
    return UNKNOWN_CHANGE


def data_of(mounts) -> tuple[Mapping[str, Any], ...]:
    """The writable mounts that hold what the container keeps."""

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
    fixes, introduces = weigh(standing, target, standing.target_advisories, standing.target_checked)
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
    )


def _found(checked: Mapping[str, Any] | None) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(str(item.get("id")), str(item.get("package"))): item for item in (checked or {}).get("findings") or ()}


def weigh(
    standing: Any, target: str, advisories, checked: Mapping[str, Any] | None
) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
    """What moving what runs (``standing``) to the version ``target`` fixes,
    and what it brings of its own: ``advisories`` are those against the target
    and ``checked`` its packages checked against OSV, None where not scanned.
    The one judge of a move, whether HQ found the release or a pull request pins it."""

    running = {str(advisory.get("id", "")) for advisory in standing.advisories}
    introduces = ranked(
        (*(advisory for advisory in advisories if advisory.get("id") not in running), *_brought(standing, checked))
    )
    return _fixed(standing, target, checked), introduces


def _fixed(standing: Any, target: str, checked: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Which of what makes the running version vulnerable the target is clear
    of: an advisory its range leaves the target out of, a package finding the
    target's checked packages no longer hold."""

    pairs = {str(item.get("id", "")): item.get("vulnerabilities") for item in standing.advisories}
    urgent = {str(item.get("id", "")) for item in standing.urgent}
    after = None if checked is None else {str(item.get("id", "")) for item in (checked.get("findings") or ())}

    def clear(item: Mapping[str, Any]) -> bool:
        key = str(item.get("id", ""))
        if key in pairs and affected(target, [tuple(pair) for pair in pairs[key] or ()]) is False:
            return True
        return after is not None and key in urgent and key not in after

    return tuple(item for item in standing.known_vulnerabilities if clear(item))


def _brought(standing: Any, checked: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Serious vulnerabilities in the target that what runs does not have."""

    if standing.checked is None or checked is None:
        return ()
    before = _found(standing.checked)
    return ranked(
        item for key, item in _found(checked).items() if key not in before and item.get("severity") in SERIOUS
    )


def _unvetted(standing: Any) -> tuple[Blocker, ...]:
    """What is not yet known about the target: its build, and its packages."""

    attested = standing.target_attested
    if attested is None or attested.get("unread"):
        return (Blocker("target-unread", "The new version's package list and build record have not been read yet."),)
    found = []
    if not attested.get("provenance"):
        found.append(
            Blocker("no-provenance", "The new version has no build provenance, so what it was built from is unknown.")
        )
    if not attested.get("packages"):
        found.append(
            Blocker(
                "no-package-list", "The new version has no package list, so it cannot be checked for vulnerabilities."
            )
        )
    elif standing.target_checked is None:
        found.append(Blocker("not-scanned", "The new version's packages have not been checked yet."))
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
        found.append(Blocker("own-pipeline", "HQ's own image is deployed by its release pipeline."))
    if not standing.target_digest:
        found.append(Blocker("no-target-digest", f"The exact build of {standing.latest} has not been read yet."))
    if not item.running.watcher:
        found.append(Blocker("not-declared", "HQ does not track this container. Adopt it first."))
    if item.mounts is None:
        found.append(Blocker("mounts-unread", "Its mounts have not been read, so the data it keeps is unknown."))
    if introduces:
        found.append(Blocker("target-affected", f"{standing.latest} brings a known vulnerability of its own."))
    return tuple(found)


def _not_automatic(change: str, verified_by, blockers, unvetted=()) -> tuple[Blocker, ...]:
    """What keeps it from applying without a person: every blocker, what is
    not known about the target, and then the bar only a vetted patch clears."""

    found = [*blockers, *unvetted]
    if change != PATCH:
        found.append(Blocker("not-a-patch", f"A {change} update waits for you."))
    if not verified_by:
        found.append(
            Blocker("unverifiable", "Nothing would show that the new version works, other than that it stays running.")
        )
    found.append(Blocker("not-opted-in", "Automatic upgrades are not turned on for this container."))
    return tuple(found)


@dataclass(frozen=True, slots=True)
class Readiness:
    """What an upgrade of this container would take, before any is published:
    what would verify it, the data it keeps, and what keeps it from
    applying on its own. The same answer a plan gives, without a target."""

    container: Container
    data: tuple[Mapping[str, Any], ...]
    verified_by: tuple[str, ...]
    blockers: tuple[Blocker, ...]
    not_automatic: tuple[Blocker, ...]

    @property
    def automatic(self) -> bool:
        return not self.not_automatic

    @property
    def data_said(self) -> Answer:
        return data_said(self.data)

    @property
    def verified_said(self) -> Answer:
        return verified_said(self.verified_by, Tone.GOOD)

    @property
    def automatic_said(self) -> Answer:
        if self.automatic:
            return Answer("Patch updates apply", tone=Tone.GOOD)
        return Answer("Not yet", (f"{counted(len(self.not_automatic), 'reason')}, listed below",))


def fixes_said(fixes: tuple[Mapping[str, Any], ...]) -> Answer:
    """What a move fixes, counted, over the first two it names."""

    return Answer(counted(len(fixes), "known vulnerability", "known vulnerabilities"), named(fixes), Tone.GOOD)


def named(advisories: tuple[Mapping[str, Any], ...]) -> tuple[EntityLink, ...]:
    """The first two of ``advisories``, each linked by its id: a fact's reasons."""

    return tuple(
        declared_link(str(each.get("id", "")), str(each.get("url", "")), "dependency") for each in advisories[:2]
    )


# Why a container's data is a reason to look before it is upgraded.
DATA_RISK = "A new version may rewrite it"


def data_said(data: tuple[Mapping[str, Any], ...], tone: Tone | Literal[""] = "") -> Answer:
    """The data a container keeps as its fact says it: how many mounts hold
    it, each named, in ``tone`` with its reason where an upgrade risks it."""

    if not data:
        return Answer("Keeps no data", ("It has no writable mount",))
    sources = tuple(Code(str(mount.get("source", ""))) for mount in data)
    return Answer(counted(len(data), "mount"), (DATA_RISK, *sources) if tone else sources, tone)


def verified_said(verified_by: tuple[str, ...], tone: Tone | Literal[""] = "") -> Answer:
    """What would show a new version works, as its fact says it: the first
    over the rest, in ``tone``; nothing, in attention, with what HQ sees instead."""

    if not verified_by:
        return Answer("Nothing", ("HQ can only see that it stays running",), Tone.ATTENTION)
    return Answer(capfirst(verified_by[0]), tuple(capfirst(what) for what in verified_by[1:]), tone)


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
        return (Blocker("attestations-unread", "Its package list and build record have not been read yet."),)
    if not attested.get("packages"):
        return (
            Blocker("no-package-list", "Its publisher provides no package list, so a new release cannot be checked."),
        )
    return ()


def plans() -> list[Plan]:
    """Every container something newer is published for: fixes first, then by
    exposure, then least risky."""

    order = (LOW, MEDIUM, HIGH)
    found = [plan for plan in (plan_for(item) for item in containers()) if plan is not None]
    # What it fixes first, then the most exposed: an advisory the internet can
    # reach outranks the same one on the tailnet (``application.exposure``).
    return sorted(
        found,
        key=lambda plan: (
            not plan.fixes,
            LEVELS.index(exposure.exposure_of_container(plan.container).level),
            order.index(plan.risk),
            plan.container.address,
        ),
    )
