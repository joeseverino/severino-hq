"""Findings about what each container runs, each with the help HQ derived.

One per container on its machine for an image version with a known advisory,
for a newer release (an update is a chore to schedule, not a fire), and for
each serious check of the standard it fails, offering its compose change
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

from .action_links import command_action, command_url
from .containers import VULNERABLE, Container, ImageStanding, containers
from .exposure import OPEN, UNKNOWN, exposure_of_container, status_at
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
from .ui import ended
from .workflow_contracts import ActionLink
lazy from .container_standard import STANDARD
lazy from .standards import UNMET
lazy from .upgrades import OVERRIDES, plan_for

# Why an update is a person's to apply.
BY_HAND = "HQ cannot apply an update itself yet."
UPDATE_CAPABILITY = "infrastructure.resource.update"
ADVISORY = "container-image-advisory"
UPDATE = "container-image-update"
HARDENING = "container-hardening"


def add(nodes, machine: Callable[[Any], str]) -> None:
    """Each container's findings, on the machine it runs on."""

    carry(nodes, (finding for item in containers() if (at := machine(item.machine.name)) for finding in _of(item, at)))


def _of(item: Container, subject: str) -> Iterator[Finding]:
    standing = item.standing
    if standing.state == VULNERABLE:
        if needs_you(standing, [item]):
            yield _advisory(item, subject)
    elif standing.newer:
        yield _found(
            item,
            subject,
            rule=UPDATE,
            severity="attention",
            title=f"{_where(item)} has an update",
            explanation=ended(f"{standing.label} to {standing.latest}") + _brings(item),
        )
    yield from _hardening(item, subject)


def needs_you(standing: ImageStanding, running: list[Container]) -> bool:
    """Whether a version asks something of you: a newer release to move to, or,
    with none, the internet may reach it. Otherwise its page says what is
    known and there is nothing to do yet."""

    return bool(standing.newer) or any(exposure_of_container(item).level in (OPEN, UNKNOWN) for item in running)


def _where(item: Container) -> str:
    return f"{item.running.name} on {item.machine.name}"


def _found(item: Container, subject: str, **said: Any) -> Finding:
    """A finding about one container: how to update it by hand, where its
    upgrade is planned, and the containers page, which helps whatever the card
    leads with."""

    primary = said.pop("primary", None) or _plan(item)
    containers = _containers()
    return Finding(
        subject=subject,
        part=item.running.name,
        steps=_by_hand(item),
        **_read(item),
        primary=primary,
        offers=() if primary.url == containers.url else (containers,),
        **said,
    )


def _containers() -> ActionLink:
    return opens("Open containers", reverse("control_plane:containers"))


def _read(item: Container, *, standing: bool = True) -> dict[str, Any]:
    """The oldest reading a finding about ``item`` is drawn from, and its
    kind: where it runs, and, for what its image stands at, the registry's."""

    found = [(item.running.observed_at, RUNTIME_KIND), (item.standing.read_at if standing else None, IMAGE_KIND)]
    at, kind = min(((at, kind) for at, kind in found if at is not None), default=(None, ""))
    return {"observed_at": at, "reads": kind}


def _upgrade_action(item: Container) -> ActionLink:
    """What an advisory card leads with: the upgrade its plan names, on the
    page that plans it, or that no release fixes it yet."""

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


def _advisory(item: Container, subject: str) -> Finding:
    """One upgrade however many advisories; none yet if no release fixes them."""

    standing = item.standing
    fixed = _fixed_in(standing)
    waiting = not standing.newer
    level, reached = _reach(item)
    fixed_in = f" {ended(f'Fixed in {fixed}')}" if fixed else ""
    return _found(
        item,
        subject,
        rule=ADVISORY,
        primary=_upgrade_action(item),
        severity=status_at("serious" if standing.serious and not waiting else "attention", level),
        title=f"{standing.label} has {standing.summary}" + (", and no release fixes it yet" if waiting else ""),
        explanation=f"Worst severity: {ended(standing.worst)} {ended(f'Running as {_where(item)}')}{fixed_in}"
        + _brings(item)
        + reached,
    )


def _brings(item: Container) -> str:
    """What a new version would bring that is worth knowing before moving to it."""

    plan = plan_for(item)
    return f" {plan.target_tag} has a known vulnerability of its own." if plan is not None and plan.introduces else ""


def _by_hand(item: Container) -> tuple[OperatorStep, ...]:
    """How it is updated today, on its machine: the image to set and the file
    that sets it, with the command that recreates the container when HQ read
    the compose project it belongs to. Without that, only what to set."""

    plan = plan_for(item)
    if plan is None:
        return ()
    name, machine = item.running.name, item.machine.name
    if item.standing.repository is not None:
        return (OperatorStep(label=f"{name} on {machine} is HQ's own image. Its release pipeline deploys it."),)
    wanted = f"{item.standing.image.name}:{plan.target_tag}"
    files = tuple(item.compose_files)
    service = str((item.runtime or {}).get("service", "") or "")
    command = (
        on_machine(
            machine,
            "docker compose "
            + " ".join(f"-f {shlex.quote(path)}" for path in files)
            + f" up -d {shlex.quote(service)}",
        )
        if files and service
        else ""
    )
    if not command:
        return (
            OperatorStep(
                label=f"On {machine}, set the image of {name} to {wanted} where it is defined, then start it again."
            ),
        )
    return (
        OperatorStep(
            label=f"On {machine}, set the image of {name} to {wanted} in {_sets_image(files)}, then run",
            command=command,
        ),
    )


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
    """Each serious check the container fails. Hardening waits on its page; reach over a machine does not."""

    for check in STANDARD:
        if check.serious and item.posture.state_of(check.id) == UNMET:
            level, reached = _reach(item)
            yield Finding(
                rule=HARDENING,
                subject=subject,
                part=item.running.name,
                severity=status_at("serious", level),
                title=f"{_where(item)} {lower_first(check.unmet)}",
                explanation=f"{check.why} {check.fix} {_help(check.id)}{reached}",
                **_read(item, standing=False),
                primary=_containers(),
                **_posture_help(check.id, item),
            )


def _reach(item: Container) -> tuple[str, str]:
    """What reaches the container, and the sentence that says so.

    What ranks a finding: the same problem is urgent where the internet
    reaches it and information where nothing routes to it.
    """

    exposure = exposure_of_container(item)
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
                    url=command_url(UPDATE_CAPABILITY, key),
                ),
            )
        }
    return {
        "offers": (
            ActionLink(
                "compose-change",
                f"Compose change for {name}",
                "read",
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
        command_url(UPDATE_CAPABILITY, key),
        target=key,
        intent=SOCKET_HOLDER_INTENT,
        reason="This container is meant to control Docker. Stop warning about it.",
    )


def _fixed_in(standing: ImageStanding) -> str:
    fixes = {
        patched.strip()
        for advisory in standing.advisories
        for _vulnerable, patched in advisory.get("vulnerabilities") or ()
        for patched in str(patched).split(",")
        if patched.strip()
    }
    return ", ".join(sorted(fixes, key=version)) if fixes else ""


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        ADVISORY,
        "Image advisories",
        "attention",
        carried(ADVISORY),
        operator_action="Move the container to a release that fixes it, once one does.",
        no_help_reason=BY_HAND,
        card=CONTAINERS_CARD,
    ),
    FindingRule(
        UPDATE,
        "Image updates",
        "attention",
        carried(UPDATE),
        operator_action="Move the container to the newer release.",
        no_help_reason=BY_HAND,
        card=CONTAINERS_CARD,
    ),
    FindingRule(
        HARDENING,
        "Container hardening",
        "serious",
        carried(HARDENING),
        operator_action="Make the compose change on the container's page.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        card=CONTAINERS_CARD,
    ),
)
