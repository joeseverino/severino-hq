"""Findings about what each container runs, each with the help HQ derived.

One per image version on its machine, naming every container that runs it,
for a known advisory, for a newer release (an update is a chore to schedule,
not a fire) and for being run by tag alone; and one per container for the
serious checks of the standard it fails, offering its compose change
(``application.container_hardening``) or, for the Docker socket, the
declaration that holding it is the container's job. Worded as the topology is
derived, from the container readings and what reaches each container, and
carried on the machine's node (``finding_model.carry``).
"""

import shlex
from collections.abc import Callable, Iterator
from typing import Any

from hq.domains.control_plane.observations.portainer import RUNTIME_KIND
from hq.domains.control_plane.observations.public_registry import IMAGE_KIND
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.routes import reverse

from .action_links import command_action, read_link
from .containers import VULNERABLE, Container, ImageStanding, containers
from .exposure import LEVELS, OPEN, UNKNOWN, exposure_of_container, status_at
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    CONTAINERS_CARD,
    Finding,
    FindingRemedy,
    FindingRule,
    OperatorStep,
    carried,
    carry,
    on_machine,
    opens,
)
from .images import version
from .labels import lower_first
from .lists import split_list
from .tone import Tone
from .ui import ended
from .workflow_contracts import ActionLink
lazy from .container_standard import STANDARD
lazy from .standards import Standing
lazy from .upgrades import OVERRIDES, plan_for

# Why an update is a person's to apply.
BY_HAND = "HQ cannot apply an update itself yet."
UPDATE_CAPABILITY = "infrastructure.resource.update"
ADVISORY = "container-image-advisory"
UPDATE = "container-image-update"
UNPINNED = "container-image-unpinned"
HARDENING = "container-hardening"


def add(nodes, machine: Callable[[Any], str]) -> None:
    """Each image's findings and each container's, on the machine they run on."""

    images: dict[tuple[str, str], list[Container]] = {}
    found: list[Finding] = []
    for item in containers():
        if at := machine(item.machine.name):
            images.setdefault((at, item.standing.label), []).append(item)
            found.extend(_hardening(item, at))
    carry(nodes, (finding for (at, _label), running in images.items() for finding in _of(running, at)))
    carry(nodes, found)


def _of(running: list[Container], subject: str) -> Iterator[Finding]:
    """What one image at one version asks of the machine that runs it, said
    once however many of its containers run it."""

    standing = running[0].standing
    if standing.state == VULNERABLE:
        if needs_you(standing, running):
            yield _advisory(running, subject)
    elif standing.newer:
        yield _found(
            running,
            subject,
            rule=UPDATE,
            severity=Tone.ATTENTION,
            title=f"{standing.label} has an update",
            explanation=ended(f"{standing.label} to {standing.latest}") + _running_as(running) + _brings(running),
        )
    if by_tag := [item for item in running if _unpinned(item.standing)]:
        yield _unpinned_finding(by_tag, subject)


def needs_you(standing: ImageStanding, running: list[Container]) -> bool:
    """Whether a version asks something of you: a newer release to move to, or,
    with none, the internet may reach it. Otherwise its page says what is
    known and there is nothing to do yet."""

    return bool(standing.newer) or any(exposure_of_container(item).level in (OPEN, UNKNOWN) for item in running)


def _names(running: list[Container]) -> str:
    return ", ".join(item.running.name for item in running)


def _running_as(running: list[Container]) -> str:
    return " " + ended(f"Running as {_names(running)} on {running[0].machine.name}")


def _lead(running: list[Container]) -> Container:
    """The container whose page plans the image's upgrade: the first HQ watches."""

    return next((item for item in running if item.running.watcher), running[0])


def _found(running: list[Container], subject: str, **said: Any) -> Finding:
    """A finding about one image on one machine: each container that runs it,
    how to update them by hand, where the upgrade is planned, and the
    containers page, which helps whatever the card leads with."""

    primary = said.pop("primary", None) or _plan(_lead(running))
    steps = said.pop("steps", None) or _by_hand(running)
    containers = _containers()
    return Finding(
        subject=subject,
        part=running[0].standing.label,
        steps=steps,
        evidence=(*(("Container", item.running.name) for item in running), *said.pop("evidence", ())),
        **_read(running),
        primary=primary,
        offers=() if primary.url == containers.url else (containers,),
        **said,
    )


def _containers() -> ActionLink:
    return opens("Open containers", reverse("control_plane:containers"))


def _read(running: list[Container], *, standing: bool = True) -> dict[str, Any]:
    """The oldest reading a finding about ``running`` is drawn from, and its
    kind: where each runs, and, for what its image stands at, the registry's."""

    found = [(item.running.observed_at, RUNTIME_KIND) for item in running]
    found.append((running[0].standing.read_at if standing else None, IMAGE_KIND))
    at, kind = min(((at, kind) for at, kind in found if at is not None), default=(None, ""))
    return {"observed_at": at, "reads": kind}


def _upgrade_action(running: list[Container]) -> ActionLink:
    """What an advisory card leads with: the upgrade its plan names, on the
    page that plans it, or that no release fixes it yet."""

    item = _lead(running)
    plan = plan_for(item)
    if plan is None:
        return opens("No fixed release to upgrade to yet", reverse("control_plane:containers"))
    known = len(item.standing.advisories) or len(item.standing.urgent)
    label = (
        f"Upgrade to {plan.target_tag} (fixes {len(plan.fixes)})"
        if plan.fixes
        else f"{plan.target_tag} is available, but is not known to fix {'it' if known == 1 else 'them'}"
    )
    return opens(label, f"{item.url}#upgrade" if item.running.watcher else reverse("control_plane:containers"))


def _advisory(running: list[Container], subject: str) -> Finding:
    """One upgrade however many advisories; none yet if no release fixes them."""

    standing = running[0].standing
    fixed = _fixed_in(standing)
    waiting = not standing.newer
    level, reached = _reach(running)
    fixed_in = f" {ended(f'Fixed in {fixed}')}" if fixed else ""
    return _found(
        running,
        subject,
        rule=ADVISORY,
        primary=_upgrade_action(running),
        severity=status_at(Tone.SERIOUS if standing.serious and not waiting else Tone.ATTENTION, level),
        title=f"{standing.label} has {standing.summary}" + (", and no release fixes it yet" if waiting else ""),
        explanation=f"Worst severity: {ended(standing.worst)}{_running_as(running)}{fixed_in}"
        + _brings(running)
        + reached,
    )


def _unpinned(standing: ImageStanding) -> bool:
    """Started by tag alone from an image a registry was read to hold, so a
    digest names a build anyone can pull. An image the registry does not
    answer for, such as one built on its machine, has no pin to name."""

    return bool(not standing.pinned and standing.digest and standing.read_at and not standing.unread)


def _pin(item: Container) -> str:
    return f"{item.running.image}@{item.standing.digest}"


def _unpinned_finding(running: list[Container], subject: str) -> Finding:
    """A tag can later name a different build, so the next pull may run one
    nobody chose; a digest always names the same one."""

    pins = tuple(dict.fromkeys(_pin(item) for item in running))
    return _found(
        running,
        subject,
        rule=UNPINNED,
        severity=Tone.ATTENTION,
        title=f"{running[0].running.image} runs by tag alone",
        explanation=f"A tag can later name a different build. Pin it by digest.{_running_as(running)}",
        evidence=tuple(("Pin it as", pin) for pin in pins),
        steps=_compose_steps(running, _pin),
        primary=_containers(),
    )


def _brings(running: list[Container]) -> str:
    """What a new version would bring that is worth knowing before moving to it."""

    plan = plan_for(_lead(running))
    return f" {plan.target_tag} has a known vulnerability of its own." if plan is not None and plan.introduces else ""


def _by_hand(running: list[Container]) -> tuple[OperatorStep, ...]:
    """How the image is updated today, on its machine: one step for each
    compose project that runs it."""

    plan = plan_for(_lead(running))
    if plan is None:
        return ()
    standing = running[0].standing
    if standing.repository is not None:
        return (
            OperatorStep(
                label=f"{_names(running)} on {running[0].machine.name} is HQ's own image. Its release pipeline deploys it."
            ),
        )
    wanted = f"{standing.image.name}:{plan.target_tag}"
    return _compose_steps(running, lambda _item: wanted)


def _compose_steps(running: list[Container], image: Callable[[Container], str]) -> tuple[OperatorStep, ...]:
    """For each compose project among ``running``, the image to set and the
    file that sets it, with the command that recreates its containers when HQ
    read the project. Without that, only what to set."""

    projects: dict[tuple[str, str], list[Container]] = {}
    for item in running:
        projects.setdefault((item.running.stack or item.running.name, image(item)), []).append(item)
    steps = []
    for (_project, wanted), members in projects.items():
        machine, names = members[0].machine.name, _names(members)
        files = tuple(members[0].compose_files)
        services = [str((item.runtime or {}).get("service", "") or "") for item in members]
        command = (
            on_machine(
                machine,
                "docker compose "
                + " ".join(f"-f {shlex.quote(path)}" for path in files)
                + " up -d "
                + " ".join(shlex.quote(service) for service in services),
            )
            if files and all(services)
            else ""
        )
        steps.append(
            OperatorStep(
                label=f"On {machine}, set the image of {names} to {wanted} in {_sets_image(files)}, then run",
                command=command,
            )
            if command
            else OperatorStep(
                label=f"On {machine}, set the image of {names} to {wanted} where it is defined, then start it again."
            )
        )
    return tuple(steps)


def _sets_image(files: tuple[str, ...]) -> str:
    """The file whose image line decides: the project's override when it has
    one, since compose reads that last, else its compose file."""

    return next((path for path in files if path.rsplit("/", 1)[-1] in OVERRIDES), files[0])


def _plan(item: Container) -> ActionLink:
    """Its upgrade plan, for a container HQ watches, since HQ plans only
    those; the containers page for any other."""

    if plan_for(item) is None or not item.running.watcher:
        return _containers()
    return opens(f"Upgrade plan for {item.running.name}", f"{item.url}#upgrade")


def _adopt(item: Container, intent: str) -> FindingRemedy:
    """Take the container on: HQ plans and hardens only what it watches."""

    return FindingRemedy(
        capability="infrastructure.resource.create",
        target=item.running.name,
        intent=intent,
        url=reverse("control_plane:adopt_record", kwargs={"kind": CONTAINER_KIND, "token": item.running.token}),
        method="POST",
    )


def _hardening(item: Container, subject: str) -> Iterator[Finding]:
    """The serious checks the container fails, as one finding about it.
    Hardening waits on its page; reach over a machine does not."""

    failed = [check for check in STANDARD if check.serious and item.posture.state_of(check.id) == Standing.UNMET]
    if not failed:
        return
    level, reached = _reach([item])
    helped = [_posture_help(check.id, item) for check in failed]
    yield Finding(
        rule=HARDENING,
        subject=subject,
        part=item.running.name,
        severity=status_at(Tone.SERIOUS, level),
        title=f"{item.running.name} on {item.machine.name} " + ", ".join(lower_first(check.unmet) for check in failed),
        explanation=" ".join(f"{check.why} {check.fix} {_help(check.id)}" for check in failed) + reached,
        **_read([item], standing=False),
        primary=_containers(),
        remedies=tuple(dict.fromkeys(remedy for said in helped for remedy in said.get("remedies", ()))),
        offers=tuple(dict.fromkeys(offer for said in helped for offer in said.get("offers", ()))),
    )


def _reach(running: list[Container]) -> tuple[str, str]:
    """What reaches the most exposed of ``running``, and the sentence that says so.

    What ranks a finding: the same problem is urgent where the internet
    reaches it and information where nothing routes to it.
    """

    exposure = min((exposure_of_container(item) for item in running), key=lambda found: LEVELS.index(found.level))
    return exposure.level, ended(f" {exposure.sentence}")


def _help(check_id: str) -> str:
    if check_id == "no-docker-socket":
        return (
            "HQ cannot tell which Docker calls it needs, so it cannot write a socket proxy. "
            "Mark it if it is meant to control Docker, and HQ stops warning about it."
        )
    return "The compose change is on its page."


def _posture_help(check_id: str, item: Container) -> dict[str, Any]:
    name = item.running.name
    if not item.running.watcher:
        then = "mark it" if check_id == "no-docker-socket" else "see its compose change"
        return {"remedies": (_adopt(item, f"Adopt {name} to {then}"),)}
    if check_id == "no-docker-socket":
        key = item.running.watcher
        return {
            "remedies": (
                FindingRemedy(
                    capability=UPDATE_CAPABILITY,
                    target=key,
                    intent=SOCKET_HOLDER_INTENT,
                ),
            )
        }
    return {
        "offers": (
            read_link(
                "compose-change",
                f"Compose change for {name}",
                f"{item.url}#hardening",
                reason="The lines to add to its compose file, and what each can break.",
            ),
        )
    }


SOCKET_HOLDER_INTENT = "Mark as meant to control Docker"


def socket_holder_link(item: Container) -> ActionLink:
    """The update of its declaration, where ``holds_docker_socket`` is set."""

    key = item.running.watcher
    return command_action(
        "mark-socket-holder",
        UPDATE_CAPABILITY,
        target=key,
        intent=SOCKET_HOLDER_INTENT,
        reason="This container is meant to control Docker. Stop warning about it.",
    )


def _fixed_in(standing: ImageStanding) -> str:
    fixes = {
        fixed
        for advisory in standing.advisories
        for _vulnerable, patched in advisory.get("vulnerabilities") or ()
        for fixed in split_list(str(patched))
    }
    return ", ".join(sorted(fixes, key=version)) if fixes else ""


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        ADVISORY,
        "Image advisories",
        Tone.ATTENTION,
        carried(ADVISORY),
        operator_action="Move the container to a release that fixes it, once one does.",
        no_help_reason=BY_HAND,
        card=CONTAINERS_CARD,
        bars={
            Tone.SERIOUS: "a container anyone may reach has a serious advisory a release fixes",
            Tone.ATTENTION: "a container has an advisory, or only a login or the tailnet stands in front of it",
            Tone.NEUTRAL: "nothing routes to the container",
        },
    ),
    FindingRule(
        UPDATE,
        "Image updates",
        Tone.ATTENTION,
        carried(UPDATE),
        operator_action="Move the container to the newer release.",
        no_help_reason=BY_HAND,
        card=CONTAINERS_CARD,
        bars={
            Tone.ATTENTION: "a newer release of a container's image is out",
            Tone.NEUTRAL: "nothing routes to the container",
        },
    ),
    FindingRule(
        UNPINNED,
        "Image run by tag alone",
        Tone.ATTENTION,
        carried(UNPINNED),
        operator_action="Pin the image by digest in the compose file that runs it and recreate its containers.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        card=CONTAINERS_CARD,
        bars={Tone.ATTENTION: "an image a registry holds is run by its tag, with no digest"},
    ),
    FindingRule(
        HARDENING,
        "Container hardening",
        Tone.SERIOUS,
        carried(HARDENING),
        operator_action="Make the compose change on the container's page.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        card=CONTAINERS_CARD,
        bars={
            Tone.SERIOUS: "a container anyone may reach fails a serious hardening check",
            Tone.ATTENTION: "a container behind a login or on the tailnet fails a serious hardening check",
            Tone.NEUTRAL: "nothing routes to the container",
        },
    ),
)
