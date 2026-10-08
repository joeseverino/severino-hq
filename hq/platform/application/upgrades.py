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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .containers import BEHIND, SERIOUS, VULNERABLE, Container, containers, ranked
from .images import affected, version

PATCH = "patch"
MINOR = "minor"
MAJOR = "major"
UNKNOWN_CHANGE = "unknown"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"


@dataclass(frozen=True, slots=True)
class Blocker:
    id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
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
    def install(self) -> tuple[Step, ...]:
        """How to give its machine the helper, while that is what stands in the way."""

        if not any(blocker.id == "no-apply-path" for blocker in self.blockers):
            return ()
        machine = self.container.machine
        return install_steps(machine.name, deployed_here=bool(getattr(machine, "runs_hq", False)))

    @property
    def install_brought(self) -> bool:
        """Whether HQ's own deploys bring the helper to its machine: only to the
        one HQ runs on, which is the one its deploy targets."""

        return bool(getattr(self.container.machine, "runs_hq", False))

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
            reasons.append(f"it is a {self.change} version change")
        if self.introduces:
            reasons.append("the new version also has a known serious vulnerability")
        if self.stateful:
            reasons.append("it keeps data")
        if not self.verified_by:
            reasons.append("nothing checks that it works afterwards")
        return "" if self.risk == LOW else " and ".join(reasons)


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
        found.append(
            Blocker("mounts-unread", "Its mounts have not been read, so its data cannot be found to snapshot.")
        )
    if introduces:
        found.append(Blocker("target-affected", f"{standing.latest} brings a known vulnerability of its own."))
    # The queue does not yet send an upgrade to the one machine it concerns,
    # with or without the helper there (``install_steps``).
    found.append(Blocker("no-apply-path", f"HQ cannot apply an upgrade on {item.machine.name} yet."))
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


OVERRIDES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)


def _pinned_in(item: Container) -> str:
    """The override the pin is written to: the stack's own when it has one,
    else a new one beside its compose file. The compose file is never edited."""

    files = tuple(item.compose_files)
    for path in files:
        if path.rsplit("/", 1)[-1] in OVERRIDES:
            return path
    if files and "/" in files[0]:
        return f"{files[0].rsplit('/', 1)[0]}/{OVERRIDES[-1]}"
    return f"The compose override of {item.running.stack or item.running.name} on {item.machine.name}"


def _steps(item: Container, target: str, digest: str, data, verified_by) -> tuple[Step, ...]:
    where = _pinned_in(item)
    pinned = f"{item.standing.image.short}:{target}, " + ("by its digest" if digest else "once its digest is read")
    steps = [Step("pull", "Pull the target by digest", pinned)]
    if data:
        steps.append(
            Step(
                "snapshot",
                "Stop it and snapshot its data",
                ", ".join(str(mount.get("source")) for mount in data),
            )
        )
    steps += [
        Step(
            "trial",
            "Run the target as the service is defined",
            "With no network and no published ports"
            + (", against a copy of its data" if data else "")
            + ", until it proves itself. The service stays stopped, and is started unchanged if it fails.",
        ),
        Step("pin", "Pin the digest in its compose override", f"{where}. The compose file itself is not edited."),
        Step("apply", "Recreate the service", f"compose up for {item.running.name} only"),
        Step("verify", "Verify", ", ".join(verified_by) if verified_by else "That it keeps running."),
        Step(
            "keep-or-roll-back",
            "Keep it, or roll back",
            (
                "The override as it was and the snapshot are restored if verification fails."
                if data
                else "The override as it was is restored if verification fails."
            ),
        ),
    ]
    return tuple(steps)


@dataclass(frozen=True, slots=True)
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


# Where each deploy syncs the helper, root-owned (scripts/upgrade-container.sh).
HELPER = "/usr/local/lib/severino-hq/scripts/upgrade-container.sh"
SUDOERS = "/etc/sudoers.d/severino-hq-upgrade"
# The one directory whose direct children the helper upgrades. The helper
# holds the same constant, and nothing a sudo caller passes can move it.
STACKS_ROOT = "/opt/apps"

# The arguments the sudo rule admits, in the helper's order and its own
# patterns: a regular expression, which sudo matches from 1.9.10. An older sudo
# reads it as a literal and admits nothing, so it fails closed.
ARGUMENTS = (
    "^--operation [A-Za-z0-9-]{1,64}"
    f" --project-dir {STACKS_ROOT}/[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}"
    " --service [A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
    " --from [A-Za-z0-9][A-Za-z0-9_./:@-]{0,254}"
    " --to [A-Za-z0-9][A-Za-z0-9_./:-]{0,200}@sha256:[0-9a-f]{64}"
    "( --tag [A-Za-z0-9_.-]{1,128})?( --wait [0-9]{1,4})?$"
)
# An empty digest: a request the rule admits and the helper refuses harmlessly
# (no such stack), which is how the check below asks sudo without running it.
_PROBE = (
    f"--operation sudo-check --project-dir {STACKS_ROOT}/example --service example "
    f"--from example/app:1 --to example/app@sha256:{'0' * 64}"
)


def sudoers_command() -> str:
    """The helper and its arguments, escaped the way sudoers reads a command's
    arguments: a comma, colon, equals sign or backslash is literal only
    behind a backslash."""

    escaped = "".join("\\" + char if char in ",:=\\" else char for char in ARGUMENTS)
    return f"{HELPER} {escaped}"


def _helper_digest() -> str:
    """The sha256 of the helper this build ships, or "" when it is not here."""

    import hashlib
    from pathlib import Path

    from django.conf import settings

    shipped = Path(settings.BASE_DIR) / "scripts" / Path(HELPER).name
    return hashlib.sha256(shipped.read_bytes()).hexdigest() if shipped.is_file() else ""


def _copy_steps(machine: str) -> tuple[Step, ...]:
    """The helper put on a machine HQ's deploys never reach: this build's
    copy, root-owned, and checked against the digest of the one HQ ships."""

    from django.conf import settings

    source = getattr(settings, "SEVERINO_HQ_SOURCE", "")
    revision = getattr(settings, "SEVERINO_HQ_REVISION", "")
    if source and revision:
        fetch = Step(
            "fetch",
            f"Take the helper from the commit this HQ was built from ({revision[:12]})",
            f"git clone {source} severino-hq && git -C severino-hq checkout --detach {revision}",
        )
    else:
        fetch = Step(
            "fetch",
            "Take the helper from a checkout of the HQ release you run",
            "HQ does not know the commit it was built from, so check out that release's "
            "commit. The helper is scripts/upgrade-container.sh in it.",
        )
    directory = HELPER.rsplit("/", 1)[0]
    digest = _helper_digest()
    verify = f" && echo '{digest}  {HELPER}' | sha256sum -c -" if digest else ""
    return (
        fetch,
        Step(
            "install",
            f"Copy it to {machine} as ./upgrade-container.sh, then install it there owned by root",
            f"sudo install -d -o root -g root -m 0755 {directory} "
            f"&& sudo install -o root -g root -m 0755 upgrade-container.sh {HELPER}{verify}",
        ),
    )


def install_steps(machine: str, *, deployed_here: bool = False) -> tuple[Step, ...]:
    """The helper's install on one machine: on the machine HQ deploys to, every
    deploy brings it; anywhere else it is copied there root-owned first. Then
    a sudo rule for it, with only the arguments it takes, and nothing else.

    HQ does not know which account the controller signs in as on a machine
    (the controller's own environment holds it), so the step names a
    placeholder and refuses to write the rule until it is replaced.
    """

    return (() if deployed_here else _copy_steps(machine)) + (
        Step(
            "allow",
            f"On {machine}, let the controller's account run it with an upgrade's arguments, and nothing else. "
            "HQ does not know which account that is: set account= to it first.",
            'account=CONTROLLER_ACCOUNT && id -u "$account" >/dev/null '
            f"&& printf '%s ALL=(root) NOPASSWD: %s\\n' \"$account\" '{sudoers_command()}' "
            f"| sudo tee {SUDOERS} >/dev/null && sudo chmod 0440 {SUDOERS} && sudo visudo -cf {SUDOERS}",
        ),
        Step("check", "Check the rule as that account", f"sudo -n -l {HELPER} {_PROBE}"),
    )


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

    from .exposure import LEVELS, exposure_of_container

    order = (LOW, MEDIUM, HIGH)
    found = [plan for plan in (plan_for(item) for item in containers()) if plan is not None]
    # What it fixes first, then the most exposed: an advisory the internet can
    # reach outranks the same one on the tailnet (``application.exposure``).
    return sorted(
        found,
        key=lambda plan: (
            not plan.fixes,
            LEVELS.index(exposure_of_container(plan.container).level),
            order.index(plan.risk),
            plan.container.address,
        ),
    )
