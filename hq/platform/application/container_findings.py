"""Findings about what each container runs, each with the help HQ derived.

One per image version on its machine for an advisory, a newer release or a bare
tag, and one per container for the hardening standard it fails
(``HARDENING_STANDARD``).
"""

import shlex
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

from django.utils.text import capfirst

from hq.domains.control_plane.observations.portainer import RUNTIME_KIND
from hq.domains.control_plane.observations.public_registry import IMAGE_KIND
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.platform.application.routes import reverse

from .action_links import command_action, read_link
from .container_standard import STANDARD
from .containers import VULNERABLE, Container, ImageStanding, containers
from .display_words import Code
from .entity_links import entity_link
from .exposure import LEVELS, OPEN, UNKNOWN, exposure_of_container, status_at
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    CONTAINERS_CARD,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    distinct,
    on_machine,
    opens,
)
from .finding_steps import Do, Run
from .images import fixed_in
from .projection import read_once
from .references import Kind
from .standard_findings import Help, Measured, Per, Standard, why
from .standards import Check
from .tone import Tone
from .ui import agreed, ended
from .workflow_contracts import ActionLink
lazy from .app_updates import UpdateKind, UpdateState, update_state
lazy from .topology_estate import machine_nodes
lazy from .upgrades import HIGH, MAJOR, Plan, plan_for

# The files a stack's override is read from, beside its compose file.
OVERRIDES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
)
# Why an update is a person's to apply: no repository holds the container's
# Compose files, so no pull request will move it.
BY_HAND = "No repository HQ reads holds its Compose files."
UPDATE_CAPABILITY = "infrastructure.resource.update"
ADVISORY = "container-image-advisory"
UPDATE = "container-image-update"
UNPINNED = "container-image-unpinned"
HARDENING = "container-hardening"


def _images(rule: str) -> Callable[[FindingEstate], tuple[Finding, ...]]:
    """The detector of ``rule``: its findings among every image's."""

    def detect(_estate: FindingEstate) -> tuple[Finding, ...]:
        found = distinct(
            finding for finding in read_once("container_findings.images", _image_findings) if finding.rule == rule
        )
        return tuple(sorted(found, key=lambda finding: finding.title))

    return detect


def _image_findings() -> tuple[Finding, ...]:
    """Each image's findings, on the machine they run on."""

    machine = machine_nodes()
    images: dict[tuple[str, str], list[Container]] = {}
    for item in containers():
        if at := machine(item.machine.name):
            images.setdefault((at, item.standing.label), []).append(item)
    return tuple(finding for (at, _label), running in images.items() for finding in _of(running, at))


def _of(running: list[Container], subject: str) -> Iterator[Finding]:
    """What one image at one version asks of the machine that runs it, said
    once however many of its containers run it."""

    standing = running[0].standing
    plan = plan_for(_lead(running))
    if standing.state == VULNERABLE:
        if needs_you(standing, running):
            yield _advisory(running, subject, plan)
    elif plan is not None and plan.recommended and not (state := update_state(_lead(running))).at_work:
        title = f"{_names(running)} has an update" + (f", {state.move}" if state.move else "")
        upgrade = _upgrade(running, plan, state)
        # What the title and its line say is not said again among its facts.
        said = f"{title} {state.words}".casefold()
        upgrade["evidence"] = tuple(fact for fact in upgrade["evidence"] if str(fact[1]).casefold() not in said)
        yield _found(
            running,
            subject,
            rule=UPDATE,
            severity=Tone.ATTENTION,
            title=title,
            explanation=state.words,
            **upgrade,
        )
    if by_tag := [item for item in running if _unpinned(item.standing)]:
        yield _unpinned_finding(by_tag, subject)


def needs_you(standing: ImageStanding, running: list[Container]) -> bool:
    """Whether a version asks something of you: an upgrade its plan
    recommends, or, a vulnerable one with none, the internet may reach it.
    Otherwise its page says what is known and there is nothing to do yet."""

    plan = plan_for(_lead(running))
    if plan is not None and plan.recommended:
        return True
    return standing.state == VULNERABLE and any(
        exposure_of_container(item).level in (OPEN, UNKNOWN) for item in running
    )


def _names(running: list[Container]) -> str:
    return ", ".join(item.running.name for item in running)


def _lead(running: list[Container]) -> Container:
    """The container whose page plans the image's upgrade: the first HQ watches."""

    return next((item for item in running if item.running.watcher), running[0])


def _found(running: list[Container], subject: str, **said: Any) -> Finding:
    """A finding about one image on one machine: each container that runs it,
    what the card leads with and the steps it gives, and the containers page,
    which helps whatever the card leads with."""

    primary = said.pop("primary", None)
    containers = _containers()
    offers = (
        *said.pop("offers", ()),
        *(() if primary is not None and primary.url == containers.url else (containers,)),
    )
    return Finding(
        subject=subject,
        part=running[0].standing.label,
        evidence=(
            *(("Container", item.link) for item in running),
            ("Machine", entity_link("machine", running[0].machine.name)),
            *said.pop("evidence", ()),
        ),
        **_read(running),
        primary=primary,
        offers=offers,
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


def _upgrade(running: list[Container], plan: Plan | None, state: UpdateState | None = None) -> dict[str, Any]:
    """What a card about moving ``running`` on does: the one action its
    update's state offers (``update_state``), else why it offers none; and
    the page that plans it with no step, for a move its plan says is not
    worth making (``Plan.recommended``)."""

    lead = _lead(running)
    # HQ plans only what it watches; the containers page stands in for any other.
    page = opens("Upgrade plan", f"{lead.url}#upgrade") if plan is not None and lead.running.watcher else _containers()
    if plan is None or not plan.recommended:
        return {"primary": page, "steps": ()}
    state = state or update_state(lead)
    evidence = (*(state.update.evidence if state.update else ()), *_risk(plan))
    if state.kind == UpdateKind.BY_HAND:
        said = [*([MAJOR] if plan.change == MAJOR else []), *([f"fixes {len(plan.fixes)}"] if plan.fixes else [])]
        label = f"Upgrade to {plan.target_tag}" + (f" ({', '.join(said)})" if said else "")
        return {"primary": opens(label, page.url), "steps": _by_hand(running, plan), "evidence": evidence}
    if state.action is None:
        # What holds it back is what to clear, so it is the one step, said once.
        return {"steps": (Do(state.reason),), "evidence": evidence, "no_help_reason": state.reason}
    return {"steps": (), "evidence": evidence, "remedies": (state.action.remedy,)}


def _held_back(standing: ImageStanding, plan: Plan | None) -> str:
    """Why a vulnerable version has no upgrade to make, as the end of its title."""

    them = agreed(len(standing.known_vulnerabilities), "it", "them")
    if plan is None:
        return f", and no release fixes {them} yet"
    if plan.introduces:
        return f", and {plan.target_tag} has a known vulnerability of its own"
    if not plan.recommended:
        return f", and {plan.target_tag} is not known to fix {them}"
    return ""


def _risk(plan: Plan) -> tuple[tuple[str, str], ...]:
    """The upgrade's risk, where it is high enough to weigh before moving."""

    return (("Upgrade", plan.risk_said),) if plan.risk == HIGH else ()


def _advisory(running: list[Container], subject: str, plan: Plan | None) -> Finding:
    """One upgrade however many advisories; none while no release fixes them."""

    standing = running[0].standing
    fixed = fixed_in(
        patched for advisory in standing.advisories for _vulnerable, patched in advisory.get("vulnerabilities") or ()
    )
    held = _held_back(standing, plan)
    level, reached = _reach(running)
    upgrade = _upgrade(running, plan)
    upgrade["evidence"] = (
        *upgrade.get("evidence", ()),
        ("Worst severity", capfirst(standing.worst)),
        *((("Fixed in", fixed),) if fixed else ()),
    )
    return _found(
        running,
        subject,
        rule=ADVISORY,
        severity=status_at(Tone.SERIOUS if standing.serious and not held else Tone.ATTENTION, level),
        title=f"{standing.label} has {standing.summary}{held}",
        explanation=reached,
        **upgrade,
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
        explanation="A tag can later name a different build.",
        evidence=tuple(("Pin it as", Code(pin)) for pin in pins),
        steps=_compose_steps(running, _pin),
        primary=_containers(),
    )


def _by_hand(running: list[Container], plan: Plan) -> tuple[Run | Do, ...]:
    """How the image is moved to the plan's target on its machine where no
    repository holds its Compose files: one step for each compose project that runs it."""

    standing = running[0].standing
    if standing.repository is not None:
        return (
            Do(f"{_names(running)} on {running[0].machine.name} is HQ's own image. Its release pipeline deploys it."),
        )
    wanted = f"{standing.image.name}:{plan.target_tag}" + (f"@{plan.target_digest}" if plan.target_digest else "")
    return _compose_steps(running, lambda _item: wanted)


def _compose_steps(running: list[Container], image: Callable[[Container], str]) -> tuple[Run | Do, ...]:
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
        if command:
            steps.append(
                Do(ended(f"On {machine}, set the image of {names} in {_sets_image(files)}"), value=f"image: {wanted}")
            )
            steps.append(Run("Then start it again", command))
        else:
            steps.append(
                Do(
                    f"On {machine}, set the image of {names} where it is defined, then start it again.",
                    value=f"image: {wanted}",
                )
            )
    return tuple(steps)


def _sets_image(files: tuple[str, ...]) -> str:
    """The file whose image line decides: the project's override when it has
    one, since compose reads that last, else its compose file."""

    return next((path for path in files if path.rsplit("/", 1)[-1] in OVERRIDES), files[0])


def _adopt(item: Container, intent: str) -> FindingRemedy:
    """Take the container on: HQ plans and hardens only what it watches."""

    return FindingRemedy(
        capability="infrastructure.resource.create",
        target=item.running.name,
        intent=intent,
        url=reverse("control_plane:adopt_record", kwargs={"kind": CONTAINER_KIND, "token": item.running.token}),
        method="POST",
    )


def _on_machines(estate: FindingEstate) -> Iterator[Measured]:
    """Each container on a machine the estate holds, measured."""

    machines = {node.id for node in estate.nodes() if node.kind == "machine"}
    for item in containers():
        if (node := Kind.MACHINE.of(item.machine.name)) in machines:
            yield Measured(
                measurement=item.posture,
                name=f"{item.running.name} on {item.machine.name}",
                node=node,
                part=item.running.name,
                reach=partial(_reached, item),
                **_read([item], standing=False),
            )


def _reached(item: Container, severity: Tone) -> tuple[Tone, str]:
    level, reached = _reach([item])
    return status_at(severity, level), reached


def _reach(running: list[Container]) -> tuple[str, str]:
    """What reaches the most exposed of ``running``, and the sentence that says so.

    What ranks a finding: the same problem is urgent where the internet
    reaches it and information where nothing routes to it.
    """

    exposure = min((exposure_of_container(item) for item in running), key=lambda found: LEVELS.index(found.level))
    return exposure.level, ended(exposure.sentence)


def _help(check_id: str) -> str:
    if check_id == "no-docker-socket":
        return "HQ cannot tell which calls it needs. Mark it if it should control Docker."
    return "The compose change is on its page."


def _posture_help(check: Check, subjects: tuple[Measured, ...]) -> Help:
    remedies: list[FindingRemedy] = []
    offers: list[ActionLink] = []
    for about in subjects:
        item = about.measurement.subject
        name = item.running.name
        if not item.running.watcher:
            remedies.append(_adopt(item, "Adopt it"))
        elif check.id == "no-docker-socket":
            remedies.append(
                FindingRemedy(capability=UPDATE_CAPABILITY, target=item.running.watcher, intent=SOCKET_HOLDER_INTENT)
            )
        else:
            offers.append(
                read_link(
                    "compose-change",
                    f"Compose change for {name}",
                    f"{item.url}#hardening",
                    reason="The lines to add to its compose file, and what each can break.",
                )
            )
    return Help(
        steps=(Do(check.fix, notes=(*why(check), _help(check.id))),), remedies=tuple(remedies), offers=tuple(offers)
    )


SOCKET_HOLDER_INTENT = "Mark as intended"


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


# Only its serious checks are raised; the rest wait on the container's page.
HARDENING_STANDARD = Standard(
    name=HARDENING,
    title="Container hardening",
    severity=Tone.SERIOUS,
    checks=tuple(check for check in STANDARD if check.serious),
    subjects=_on_machines,
    raises=Per.SUBJECT,
    operator_action="Make the compose change on the container's page.",
    no_help_reason=CANNOT_EDIT_COMPOSE,
    bars={
        Tone.SERIOUS: "a container anyone may reach fails a serious hardening check",
        Tone.ATTENTION: "a container behind a login or on the tailnet fails a serious hardening check",
        Tone.NEUTRAL: "nothing routes to the container",
    },
    primary=_containers,
    helps=_posture_help,
)

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        ADVISORY,
        "Image advisories",
        Tone.ATTENTION,
        _images(ADVISORY),
        gathered=True,
        operator_action="Move it to a release that fixes it.",
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
        _images(UPDATE),
        gathered=True,
        operator_action="Move it to the newer release.",
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
        _images(UNPINNED),
        gathered=True,
        operator_action="Pin it by digest in its compose file, then recreate it.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        card=CONTAINERS_CARD,
        bars={Tone.ATTENTION: "an image a registry holds is run by its tag, with no digest"},
    ),
    HARDENING_STANDARD.rule(card=CONTAINERS_CARD),
)
