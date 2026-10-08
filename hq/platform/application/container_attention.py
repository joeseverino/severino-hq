"""What needs doing about the containers, each item with the help HQ derived.

A version with a known advisory is one item per image and version, saying what
upgrade clears it and, when nothing can apply that upgrade yet, exactly why.
Every image with a newer release is one item, because updates are a chore to
schedule rather than a fire. Each serious check a container fails is one item,
offering per container the compose change HQ wrote for it
(``application.container_hardening``) or, for the Docker socket, the
declaration that says holding it is the container's job.
"""

import shlex
from typing import Any

from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.routes import reverse

from .action_links import command_url
from .containers import VULNERABLE, Container, Standing, containers
from .exposure import LEVELS, OPEN, UNKNOWN, UNROUTED, exposure_of_container, status_at, worse
from .finding_model import on_machine
from .images import version
from .item_help import do_step, run_step, steps
from .ui import Insight, counted
from .workflow_contracts import ActionLink, WorkflowPlan, WorkflowStep

# Said once on a card whose update is applied by hand.
BY_HAND = "HQ cannot apply an update itself yet."
UPDATE_CAPABILITY = "infrastructure.resource.update"
# Buttons on one item before the rest are left to the containers page.
MOST_ACTIONS = 8


def attention() -> tuple[Insight, ...]:
    seen: dict[str, tuple[Standing, list[Container]]] = {}
    for item in containers():
        seen.setdefault(item.standing.label, (item.standing, []))[1].append(item)
    items = []
    behind = []
    behind_running: list[Container] = []
    for label, (standing, running) in seen.items():
        if standing.state == VULNERABLE:
            if needs_you(standing, running):
                items.append(_advisory(label, standing, running))
        elif standing.newer:
            behind.append(f"{label} → {standing.latest}")
            behind_running.extend(running)
    items.extend(_reach_attention())
    if behind:
        items.append(
            Insight(
                status="attention",
                eyebrow="Containers",
                key="container-updates",
                title=f"{counted(len(behind), 'running image has', 'running images have')} an update",
                value=str(len(behind)),
                magnitude=len(behind),
                body=", ".join(sorted(behind)) + "." + _brings(behind_running),
                action="Open containers",
                url=reverse("control_plane:containers"),
                workflow=by_hand("container-updates", behind_running),
                # Each one's plan, the same help an advisory carries.
                actions=_limited(_upgrade_link(item) for item in behind_running),
            )
        )
    return tuple(items)


def needs_you(standing: Standing, running: list[Container]) -> bool:
    """Whether a version asks something of you: a newer release to move to, or,
    with none, the internet may reach it. Otherwise its page says what is
    known and there is nothing to do yet."""

    return bool(standing.newer) or _reach(running)[0] in (OPEN, UNKNOWN)


def _plans(running: list[Container]) -> list[Any]:
    from .upgrades import plan_for

    return [plan for plan in (plan_for(item) for item in running) if plan is not None]


def _brings(running: list[Container]) -> str:
    """What a new version would bring that is worth knowing before moving to it."""

    brings = sorted({plan.target_tag for plan in _plans(running) if plan.introduces})
    return "".join(f" {tag} has a known vulnerability of its own." for tag in brings)


def by_hand(key: str, running: list[Container]) -> WorkflowPlan | None:
    """How these are updated today: on each machine, one step per container.

    The step names the image to set and the file that sets it, and carries the
    command that recreates the container, when HQ read the compose project it
    belongs to. Without that it says only what to set.
    """

    told = [_by_hand_step(plan) for plan in _plans(running)]
    return steps(key, told, reason=BY_HAND) if told else None


def _by_hand_step(plan: Any) -> WorkflowStep:
    item = plan.container
    name, machine = item.running.name, item.machine.name
    if item.standing.repository is not None:
        return do_step(f"{name} on {machine} is HQ's own image. Its release pipeline deploys it.")
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
        return do_step(f"On {machine}, set the image of {name} to {wanted} where it is defined, then start it again.")
    return run_step(f"On {machine}, set the image of {name} to {wanted} in {_sets_image(files)}, then run", command)


def _sets_image(files: tuple[str, ...]) -> str:
    """The file whose image line decides: the project's override when it has
    one, since compose reads that last, else its compose file."""

    from .upgrades import OVERRIDES

    return next((path for path in files if path.rsplit("/", 1)[-1] in OVERRIDES), files[0])


def _advisory(label: str, standing: Standing, running: list[Container]) -> Insight:
    """One upgrade however many advisories; none yet if no release fixes them."""

    where = ", ".join(sorted({f"{item.running.name} on {item.machine.name}" for item in running}))
    fixed = _fixed_in(standing)
    known = len(standing.advisories) or len(standing.urgent)
    waiting = not standing.newer
    plans = _plans(running)
    key = f"container-advisory:{standing.image.name}:{standing.tag}"
    body = f"Worst severity: {standing.worst}. Running as {where}.{f' Fixed in {fixed}.' if fixed else ''}"
    body += _brings(running)
    watched = next((item for item in running if item.running.watcher), None)
    level, reached = _reach(running)
    body += reached
    return Insight(
        status=status_at("serious" if standing.serious and not waiting else "attention", level),
        eyebrow="Containers",
        family="Image advisories",
        key=key,
        title=f"{label} has {standing.summary}" + (", and no release fixes it yet" if waiting else ""),
        value=str(known),
        magnitude=1,
        body=body,
        action=_upgrade_action(plans[0], known) if plans else "No fixed release to upgrade to yet",
        url=f"{watched.url}#upgrade" if watched else reverse("control_plane:containers"),
        workflow=by_hand(key, running),
        actions=_limited(_upgrade_link(item) for item in running) if plans else (),
    )


def _upgrade_action(plan: Any, known: int) -> str:
    if plan.fixes:
        return f"Upgrade to {plan.target_tag} (fixes {len(plan.fixes)})"
    return f"{plan.target_tag} is available, but is not known to fix {'it' if known == 1 else 'them'}"


def _upgrade_link(item: Container) -> ActionLink:
    if item.running.watcher:
        return ActionLink(
            "upgrade-plan",
            f"Upgrade plan for {item.running.name}",
            "read",
            f"{item.url}#upgrade",
            reason="The steps to upgrade it, and what blocks them.",
        )
    return _adopt(item, f"Adopt {item.running.name} to plan its upgrade")


def _adopt(item: Container, label: str) -> ActionLink:
    """Take the container on: HQ plans and hardens only what it watches."""

    return ActionLink(
        "adopt",
        label,
        "remote_write",
        reverse("control_plane:adopt_record", kwargs={"kind": CONTAINER_KIND, "token": item.running.token}),
        method="POST",
        reason="Nothing changes on the machine. HQ starts tracking the container.",
    )


def _limited(actions) -> tuple[ActionLink, ...]:
    return tuple(actions)[:MOST_ACTIONS]


def _reach_attention() -> list[Insight]:
    """One item per serious check a container fails, naming every container
    that fails it. Hardening waits on the page; reach over a machine does not."""

    from .container_standard import STANDARD
    from .standards import UNMET

    found = containers()
    items = []
    for check in STANDARD:
        if not check.serious:
            continue
        failing = sorted(
            (item for item in found if item.posture.state_of(check.id) == UNMET),
            key=lambda item: (
                LEVELS.index(exposure_of_container(item).level),
                item.running.name,
                item.machine.name,
            ),
        )
        if failing:
            level, reached = _reach(failing)
            items.append(
                Insight(
                    status=status_at("serious", level),
                    eyebrow="Containers",
                    family="Container hardening",
                    key=f"container-posture:{check.id}",
                    title=f"{check.label}: {counted(len(failing), 'container fails', 'containers fail')}",
                    value=str(len(failing)),
                    magnitude=len(failing),
                    body=f"{', '.join(f'{item.running.name} on {item.machine.name}' for item in failing)}. "
                    f"{check.why} {check.fix} {_help(check.id)}{reached}",
                    action="Open containers",
                    url=reverse("control_plane:containers"),
                    actions=_limited(_posture_link(check.id, item) for item in failing),
                )
            )
    return items


def _reach(running) -> tuple[str, str]:
    """The worst exposure among ``running``, and the sentence that says so.

    What ranks an item: the same problem is urgent where the internet reaches
    it and information where nothing routes to it.
    """

    level = UNROUTED
    worst = None
    for item in running:
        exposure = exposure_of_container(item)
        if worst is None or worse(exposure.level, level) != level:
            level, worst = exposure.level, (item, exposure)
    if worst is None:
        return UNROUTED, ""
    item, exposure = worst
    return (
        level,
        f" Most exposed: {item.running.name} on {item.machine.name}, {exposure.sentence[:1].lower()}{exposure.sentence[1:]}.",
    )


def _help(check_id: str) -> str:
    if check_id == "no-docker-socket":
        return (
            "HQ cannot tell which Docker calls each needs, so it cannot write a socket proxy for it. "
            "For a container that is meant to control Docker, say so and HQ stops warning about it."
        )
    return "The compose change for each is on its page."


def _posture_link(check_id: str, item: Container) -> ActionLink:
    name = item.running.name
    if not item.running.watcher:
        then = "mark it" if check_id == "no-docker-socket" else "see its compose change"
        return _adopt(item, f"Adopt {name} to {then}")
    if check_id == "no-docker-socket":
        return socket_holder_link(item)
    return ActionLink(
        "compose-change",
        f"Compose change for {name}",
        "read",
        f"{item.url}#hardening",
        reason="The lines to add to its compose file, and what each can break.",
    )


def socket_holder_link(item: Container) -> ActionLink:
    """The update of its declaration, where ``holds_docker_socket`` is set."""

    key = item.running.watcher
    return ActionLink(
        "mark-socket-holder",
        f"Mark {item.running.name} as meant to control Docker",
        "remote_write",
        command_url(UPDATE_CAPABILITY, key),
        capability=UPDATE_CAPABILITY,
        target=key,
        reason="This container is meant to control Docker. Stop warning about it.",
    )


def _fixed_in(standing: Standing) -> str:
    fixes = {
        patched.strip()
        for advisory in standing.advisories
        for _vulnerable, patched in advisory.get("vulnerabilities") or ()
        for patched in str(patched).split(",")
        if patched.strip()
    }
    return ", ".join(sorted(fixes, key=version)) if fixes else ""
