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

from hq.domains.control_plane.observations.portainer import CONTAINER_KIND, RUNTIME_KIND
from hq.domains.control_plane.observations.public_registry import IMAGE_KIND
from hq.platform.application.routes import reverse

from .container_standard import STANDARD
from .containers import VULNERABLE, Container, ImageStanding, containers
from .display_words import Code
from .entity_links import declared_link, entity_link
from .exposure import LEVELS, OPEN, UNKNOWN, exposure_of_container, status_at
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    CONTAINERS_CARD,
    Fact,
    Finding,
    FindingEstate,
    FindingRule,
    distinct,
    opens,
)
from .finding_steps import Advice, ByHand, Do, Does, Why
from .images import fixed_in
from .projection import read_once
from .references import Kind
from .standard_findings import Help, Measured, Per, Standard
from .standards import Check
from .tone import Tone
from .ui import agreed, ended
from .workflow_contracts import ActionLink
lazy from .app_updates import UpdateKind, UpdateState, update_state
lazy from .asks import FAILED
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
    elif _moves(standing, plan) and _offered(standing, state := update_state(_lead(running))):
        title = f"{_names(running)} has an update" + (f", {state.move}" if state.move else "")
        upgrade = _upgrade(running, plan, state)
        # Once a press is asked, its card says where that stands, in the
        # words its page does; while its run is under way, the run's own
        # standing says it (``Finding.under_way``).
        at = state.flow.at
        under_way = at.standing if at is not None and at.live and state.at_work else None
        asked = state.at_work or (at is not None and at.standing.state == FAILED)
        explanation = ended(state.flow.said) if asked and not under_way else state.words
        # What the title and its line say is not said again among its facts;
        # a link opens a page, which no words say.
        said = f"{title} {explanation}".casefold()
        upgrade["evidence"] = tuple(
            fact for fact in upgrade["evidence"] if not isinstance(fact[1], str) or fact[1].casefold() not in said
        )
        yield _found(
            running,
            subject,
            rule=UPDATE,
            severity=Tone.ATTENTION,
            title=title,
            explanation=explanation,
            under_way=under_way,
            **upgrade,
        )
    if by_tag := [item for item in running if _unpinned(item.standing)]:
        yield _unpinned_finding(by_tag, subject)


def _moves(standing: ImageStanding, plan: Plan | None) -> bool:
    """Whether an update may be due: a move its plan recommends, or, for an
    image built on its host, whatever its repository says."""

    return standing.built_here or (plan is not None and plan.recommended)


def _offered(standing: ImageStanding, state: UpdateState) -> bool:
    """Whether ``state`` is an update to raise: any, for a pulled image a
    plan recommends moving; for one built on its host, a pull request that
    changes it or a merge its host has not built."""

    return not standing.built_here or state.kind in (UpdateKind.OPEN, UpdateKind.MERGED)


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
    if not _moves(lead.standing, plan):
        return {"primary": page}
    state = state or update_state(lead)
    evidence = (
        *(state.update.evidence if state.update else ()),
        *(_risk(plan) if plan else ()),
        *_standing(state),
    )
    if state.kind == UpdateKind.BY_HAND and plan is not None:
        said = [*([MAJOR] if plan.change == MAJOR else []), *([f"fixes {len(plan.fixes)}"] if plan.fixes else [])]
        label = f"Upgrade to {plan.target_tag}" + (f" ({', '.join(said)})" if said else "")
        return {"primary": opens(label, page.url), "fixes": _by_hand(running, plan), "evidence": evidence}
    if state.offered is None or state.at_work:
        # What holds it back is what to clear, so it is the one step, said once.
        reason = state.reason or ended(state.flow.said)
        return {"fixes": (Do(reason),), "evidence": evidence, "no_help_reason": reason}
    return {"evidence": evidence, "fixes": (state.offered.remedy,)}


def _standing(state: UpdateState) -> tuple[Fact, ...]:
    """Where what HQ was asked stands once it is not under way: the step it
    stopped at, else its last, linked to the page that holds it, over its sentence."""

    flow = state.flow
    step = flow.at if flow.at is not None and flow.at.standing.state == FAILED else None
    if step is None and state.at_work and flow.steps and not flow.live:
        step = flow.steps[-1]
    if step is None:
        return ()
    said = step.standing
    lead = declared_link(said.label, said.url) if said.url else ended(said.label)
    note = (ended(said.note),) if said.note else ()
    return tuple((step.title, each) for each in (lead, *note, *((said.stated,) if said.stated else ())))


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
        fixes=_compose_steps(running, _pin),
        primary=_containers(),
    )


def _by_hand(running: list[Container], plan: Plan) -> tuple[Advice, ...]:
    """How the image is moved to the plan's target on its machine where no
    repository holds its Compose files: one step for each compose project that runs it."""

    standing = running[0].standing
    if standing.repository is not None:
        return (
            Do(f"{_names(running)} on {running[0].machine.name} is HQ's own image. Its release pipeline deploys it."),
        )
    wanted = f"{standing.image.name}:{plan.target_tag}" + (f"@{plan.target_digest}" if plan.target_digest else "")
    return _compose_steps(running, lambda _item: wanted)


def _compose_steps(running: list[Container], image: Callable[[Container], str]) -> tuple[Advice, ...]:
    """For each compose project among ``running``, the image to set and the
    file that sets it, with the command that recreates its containers when HQ
    read the project. Without that, only what to set."""

    projects: dict[tuple[str, str], list[Container]] = {}
    for item in running:
        projects.setdefault((item.running.stack or item.running.name, image(item)), []).append(item)
    steps: list[Advice] = []
    for (_project, wanted), members in projects.items():
        machine, names = members[0].machine.name, _names(members)
        files = tuple(members[0].compose_files)
        services = [str((item.runtime or {}).get("service", "") or "") for item in members]
        start = (
            ByHand.on(
                machine,
                "Then start it again",
                "docker compose "
                + " ".join(f"-f {shlex.quote(path)}" for path in files)
                + " up -d "
                + " ".join(shlex.quote(service) for service in services),
                Why.NOT_GRANTED,
            )
            if files and all(services)
            else ()
        )
        if start:
            steps.append(
                Do(ended(f"On {machine}, set the image of {names} in {_sets_image(files)}"), value=f"image: {wanted}")
            )
            steps.extend(start)
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


def _adopt(item: Container, intent: str) -> Does:
    """Take the container on: HQ plans and hardens only what it watches."""

    return Does(
        "infrastructure.resource.create",
        item.running.name,
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


def help_for(item: Container, check: Check) -> Help:
    """What a finding that ``item`` fails ``check`` offers, on its page and on
    Needs you alike: the compose change that meets it, and the update that
    adopts it or marks it as holding the socket."""

    if not item.running.watcher:
        remedies: tuple[Does, ...] = (_adopt(item, "Adopt it"),)
    elif check.id == "no-docker-socket":
        remedies = (Does(UPDATE_CAPABILITY, item.running.watcher, intent=SOCKET_HOLDER_INTENT),)
    else:
        remedies = ()
    step = item.hardening.step(check)
    return Help(fixes=(*remedies, *((step,) if step else ())))


def _posture_help(check: Check, subjects: tuple[Measured, ...]) -> Help:
    helps = [help_for(about.measurement.subject, check) for about in subjects]
    return Help(fixes=tuple(fix for help in helps for fix in help.fixes))


SOCKET_HOLDER_INTENT = "Mark as intended"


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
