"""What needs doing about the containers, each item with the help HQ derived.

A version with a known advisory is one item per image and version, saying what
upgrade clears it and, when nothing can apply that upgrade yet, exactly why.
Every image with a newer release is one item, because updates are a chore to
schedule rather than a fire. Each serious check a container fails is one item,
offering per container the compose change HQ wrote for it
(``application.container_hardening``) or, for the Docker socket, the
declaration that says holding it is the container's job.
"""

from __future__ import annotations

from typing import Any

from django.urls import reverse

from control_plane.provider_adapters.portainer import CONTAINER_KIND

from .containers import VULNERABLE, Container, Standing, containers
from .images import version
from .ui import Insight, counted
from .workflow_contracts import ActionLink

UPDATE_CAPABILITY = "infrastructure.resource.update"
# Buttons on one item before the rest are left to the containers page.
MOST_ACTIONS = 8


def attention() -> tuple[Insight, ...]:
    seen: dict[str, tuple[Standing, list[Container]]] = {}
    for item in containers():
        seen.setdefault(item.standing.label, (item.standing, []))[1].append(item)
    items = []
    behind = []
    for label, (standing, running) in seen.items():
        if standing.state == VULNERABLE:
            items.append(_advisory(label, standing, running))
        elif standing.newer:
            behind.append(f"{label} → {standing.latest}")
    items.extend(_reach_attention())
    if behind:
        items.append(
            Insight(
                status="attention",
                eyebrow="Containers",
                key="container-updates",
                title=f"{counted(len(behind), 'running image has', 'running images have')} a newer release",
                value=str(len(behind)),
                magnitude=len(behind),
                body="; ".join(sorted(behind)) + ".",
                action="Open containers",
                url=reverse("control_plane:containers"),
            )
        )
    return tuple(items)


def _advisory(label: str, standing: Standing, running: list[Container]) -> Insight:
    """One upgrade however many advisories; none yet if no release fixes them."""

    from .upgrades import plan_for

    where = ", ".join(sorted({f"{item.running.name} on {item.machine.name}" for item in running}))
    fixed = _fixed_in(standing)
    known = len(standing.advisories) or len(standing.urgent)
    waiting = not standing.newer
    plans = [plan for plan in (plan_for(item) for item in running) if plan is not None]
    body = f"Worst: {standing.worst}. Runs as {where}.{f' Fixed in {fixed}.' if fixed else ''}"
    reasons = list(dict.fromkeys(blocker.reason for plan in plans for blocker in plan.blockers))
    if reasons:
        body += f" Not yet, because: {'; '.join(reason.rstrip('.') for reason in reasons)}."
    watched = next((item for item in running if item.running.watcher), None)
    return Insight(
        status="serious" if standing.serious and not waiting else "attention",
        eyebrow="Containers",
        key=f"container-advisory:{standing.image.name}:{standing.tag}",
        title=f"{label} has {standing.summary}" + ("; no release fixes it yet" if waiting else ""),
        value=str(known),
        magnitude=1,
        body=body,
        action=_upgrade_action(plans[0], known) if plans else "Nothing to upgrade to until a release fixes it",
        url=f"{watched.url}#upgrade" if watched else reverse("control_plane:containers"),
        actions=_limited(_upgrade_link(item) for item in running) if plans else (),
    )


def _upgrade_action(plan: Any, known: int) -> str:
    if plan.fixes:
        return f"Upgrade to {plan.target_tag} (clears {len(plan.fixes)})"
    return f"{plan.target_tag} is out, but is not known to clear {'it' if known == 1 else 'them'}"


def _upgrade_link(item: Container) -> ActionLink:
    if item.running.watcher:
        return ActionLink(
            "upgrade-plan", f"Upgrade plan for {item.running.name}", "read", f"{item.url}#upgrade",
            reason="What upgrading it would take, and what stands in the way.",
        )
    return _adopt(item, f"Adopt {item.running.name} to plan its upgrade")


def _adopt(item: Container, label: str) -> ActionLink:
    """Take the container on: HQ plans and hardens only what it watches."""

    return ActionLink(
        "adopt", label, "remote_write",
        reverse("control_plane:adopt_record", kwargs={"kind": CONTAINER_KIND, "token": item.running.token}),
        method="POST",
        reason="Nothing changes on the machine: HQ starts watching it.",
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
            key=lambda item: (item.running.name, item.machine.name),
        )
        if failing:
            items.append(
                Insight(
                    status="serious",
                    eyebrow="Containers",
                    key=f"container-posture:{check.id}",
                    title=f"{check.label}: not met by {counted(len(failing), 'container', 'containers')}",
                    value=str(len(failing)),
                    magnitude=len(failing),
                    body=f"{', '.join(f'{item.running.name} on {item.machine.name}' for item in failing)}. "
                    f"{check.why} {check.fix} {_help(check.id)}",
                    action="Open containers",
                    url=reverse("control_plane:containers"),
                    actions=_limited(_posture_link(check.id, item) for item in failing),
                )
            )
    return items


def _help(check_id: str) -> str:
    if check_id == "no-docker-socket":
        return ("HQ cannot write the proxy: which Docker calls each makes is not in its inspect. "
                "One whose job is Docker is marked as holding the socket instead.")
    return "HQ wrote the compose change for each, on its page."


def _posture_link(check_id: str, item: Container) -> ActionLink:
    name = item.running.name
    if not item.running.watcher:
        then = "mark it" if check_id == "no-docker-socket" else "see its compose change"
        return _adopt(item, f"Adopt {name} to {then}")
    if check_id == "no-docker-socket":
        return socket_holder_link(item)
    return ActionLink(
        "compose-change", f"Compose change for {name}", "read", f"{item.url}#hardening",
        reason="The lines to add to its compose file, and what each can break.",
    )


def socket_holder_link(item: Container) -> ActionLink:
    """The update of its declaration, where ``holds_docker_socket`` is set."""

    key = item.running.watcher
    return ActionLink(
        "mark-socket-holder",
        f"Mark {item.running.name} as holding the socket (a socket proxy or management agent)",
        "remote_write",
        f"{reverse('command', kwargs={'name': UPDATE_CAPABILITY})}?target={key}",
        capability=UPDATE_CAPABILITY,
        target=key,
        reason="Set holds_docker_socket on its declaration; the socket stays listed, never an action item.",
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
