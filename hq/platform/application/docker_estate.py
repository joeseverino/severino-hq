"""The Docker readings joined into the relation graph, and what they imply.

A container no compose project declares, a container running an image its
own tag no longer names locally, an image with no tag at all and the images
nothing runs or keeps are each read from the records as the findings are
gathered, about the machine they are on. Nothing here calls a registry:
"behind" means behind the image already pulled onto the machine.
"""

import shlex
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from django.conf import settings
lazy from django.urls import NoReverseMatch

from hq.domains.control_plane.observations.portainer import CONTAINER_KIND, IMAGE_KIND, image_title, short_id
lazy from hq.platform.application.routes import reverse

from .display_words import Code
from .entity_links import container_link, entity_link
from .facts import inventory_records
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    CONTAINERS_CARD,
    Finding,
    FindingEstate,
    FindingRemedy,
    FindingRule,
    cannot_run_commands,
    distinct,
    machine_step,
)
from .finding_steps import Do, Run
from .images import ImageRef
from .labels import human_bytes
from .projection import read_once
from .tone import Tone
from .ui import counted, ended
lazy from .adoption import unmanaged
lazy from .hq_self import own_container
lazy from .inventory import record_token
lazy from .topology_estate import machine_nodes

IMAGE_BEHIND = "image-behind"
IMAGE_UNTAGGED = "image-untagged"
IMAGES_UNUSED = "container-images-unused"
# The images the running release pins, each a file of one reference in its
# tree: the ones hq-host runs only from the copy the host holds.
PINNED = ("automation", "renovate")


def reference_tag(reference: str) -> str:
    """A container's image reference as Docker tags it: ``:latest`` when none is named."""

    text = str(reference or "")
    if not text or text.startswith("sha256:") or "@" in text:
        return ""
    return text if ":" in text.rsplit("/", 1)[-1] else f"{text}:latest"


def image_verdicts(records: Iterable[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """``(fact, container, reference, running id, tagged id, service)`` for each container
    on an image the machine's tag no longer names, or on an untagged image.

    ``records`` are one environment's image records.
    """

    records = list(records)
    tagged = {tag: str(record.get("id", "")) for record in records for tag in record.get("tags") or ()}
    found = []
    for record in records:
        image = str(record.get("id", ""))
        for user in record.get("containers") or ():
            container = str(user.get("container", ""))
            reference = str(user.get("reference", ""))
            service = str(user.get("service", ""))
            # A pull moves the tag and leaves the old image untagged, so the
            # tag is asked first: that container is behind, not untagged.
            current = tagged.get(reference_tag(reference), "")
            if current and current != image:
                found.append((IMAGE_BEHIND, container, reference, image, current, service))
            elif not record.get("tags"):
                found.append((IMAGE_UNTAGGED, container, reference, image, "", service))
    return found


def image_names(record: Mapping[str, Any]) -> frozenset[str]:
    """Every image name an image record is held under, by its tags and its digests."""

    return frozenset(
        parsed.name
        for reference in (*(record.get("tags") or ()), *(record.get("digests") or ()))
        if (parsed := ImageRef.parse(str(reference))) is not None
    )


def kept_names() -> frozenset[str]:
    """The images HQ keeps on its machine with nothing running them: its own,
    whose earlier releases a deploy prunes itself, and each image its release
    pins (``PINNED``), which the worker and the dependency run start only from
    the copy the host holds."""

    return read_once("docker_estate.kept_names", _kept_names)


def _kept_names() -> frozenset[str]:
    own = own_container()
    references = (*((own.running.image,) if own is not None else ()), *release_pins())
    return frozenset(parsed.name for reference in references if (parsed := ImageRef.parse(reference)) is not None)


def release_pins() -> tuple[str, ...]:
    """The image references the running release pins (``PINNED``)."""

    pins = (settings.BASE_DIR / "deploy" / part / "image" for part in PINNED)
    return tuple(pin.read_text().strip() for pin in pins if pin.is_file())


def reclaimable(records: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """The image records no container runs and HQ does not keep (``kept_names``)."""

    kept = kept_names()
    return tuple(record for record in records if not record.get("containers") and not kept & image_names(record))


def _removed_by(record: Mapping[str, Any]) -> tuple[str, ...]:
    """What ``docker image rm`` removes this image and nothing else by: its
    tags, else the digests it was pulled by, else its id."""

    return tuple(record.get("tags") or record.get("digests") or (short_id(str(record.get("id", ""))),))


# ----- Findings ------------------------------------------------------------------


def unrecognized_containers(estate: FindingEstate) -> tuple[Finding, ...]:
    """A container a sweep found that no compose project declares.

    Every container that belongs on a machine is created by a compose project,
    and HQ takes those on by itself. Anything else was started by hand or by
    something HQ does not know, so it is reported rather than adopted: a
    container nobody declared must never look like one somebody did.
    """

    nodes = {node.id: node for node in estate.nodes()}
    machine = machine_nodes()
    found: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for item in unmanaged():
        if item.adoptable or item.kind != CONTAINER_KIND:
            continue
        name, host = str(item.spec.get("name", "")), str(item.spec.get("host", ""))
        # The sweep did not adopt it, so it has no node of its own; its machine stands for it.
        if (node := nodes.get(machine(host))) is not None and (node.id, name, host) not in seen:
            seen.add((node.id, name, host))
            try:
                adopt_url = reverse(
                    "control_plane:adopt_record",
                    args=[
                        CONTAINER_KIND,
                        record_token(CONTAINER_KIND, (host, name)),
                    ],
                )
            except NoReverseMatch:
                adopt_url = ""
            found.append(
                Finding(
                    rule="unrecognized-container",
                    subject=node.id,
                    title=f"Unrecognized container {name} on {node.label}",
                    severity=Tone.SERIOUS,
                    explanation="No compose project started it.",
                    steps=machine_step(
                        f"If nothing needs it, remove it from {host or node.label}",
                        host or node.label,
                        f"docker rm -f {shlex.quote(name)}",
                    ),
                    part=name,
                    evidence=(
                        ("Container", container_link(node.label, name)),
                        ("Machine", entity_link("machine", node.label)),
                    ),
                    remedies=(
                        FindingRemedy(
                            capability="infrastructure.resource.create",
                            target=name,
                            intent="Adopt it",
                            url=adopt_url,
                            method="POST",
                        ),
                    ),
                )
            )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _environments(estate: FindingEstate) -> Iterator[tuple[Any, str, list[Mapping[str, Any]]]]:
    """``(node, host, records)``: each Docker environment's image records, on a
    machine of the estate."""

    nodes = {node.id: node for node in estate.nodes()}
    machine = machine_nodes()
    by_environment: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for _snapshot, record in inventory_records(IMAGE_KIND):
        if record.get("id"):
            by_environment.setdefault((str(record.get("connection_ref", "")), record.get("environment_id")), []).append(
                record
            )
    for records in by_environment.values():
        first = records[0]
        if (node := nodes.get(machine(first.get("host")) or machine(first.get("host_address")))) is not None:
            yield node, str(first.get("host", "") or node.label), records


def _stated(estate: FindingEstate, key: str) -> Iterator[tuple[Any, ...]]:
    """``(node, container, host, reference, running, tagged, service)`` for each
    container on a machine of the estate whose image is judged ``key``."""

    for node, host, records in _environments(estate):
        for fact, container, reference, running, tagged, service in image_verdicts(records):
            if fact == key:
                yield node, container, host, Code(reference), Code(short_id(running)), Code(short_id(tagged)), service


def images_unused(estate: FindingEstate) -> tuple[Finding, ...]:
    """The images on a machine nothing runs and HQ does not keep, and the one
    command that removes those and nothing else."""

    found = []
    for node, host, records in _environments(estate):
        if spare := sorted(reclaimable(records), key=image_title):
            size = human_bytes(sum(record.get("size") or 0 for record in spare))
            found.append(
                Finding(
                    rule=IMAGES_UNUSED,
                    subject=node.id,
                    title=f"{counted(len(spare), 'unused image', 'unused images')} on {node.label}",
                    severity=Tone.NEUTRAL,
                    explanation=ended(f"They take {size}"),
                    evidence=tuple(("Image", Code(image_title(record))) for record in spare),
                    steps=machine_step(
                        f"Remove them from {host}",
                        host,
                        "docker image rm "
                        + " ".join(shlex.quote(ref) for record in spare for ref in _removed_by(record)),
                    ),
                )
            )
    return distinct(found)


def images_behind(estate: FindingEstate) -> tuple[Finding, ...]:
    """A container still running the image its tag named before the last pull."""

    return distinct(
        Finding(
            rule="container-image-behind",
            subject=node.id,
            title=f"{container} on {node.label} runs an older {reference}",
            severity=Tone.ATTENTION,
            explanation="Recreate it to run what was pulled.",
            part=container,
            evidence=(
                ("Container", container_link(node.label, container)),
                ("Image reference", reference),
                ("Running", running),
                ("Tagged now", tagged),
            ),
            steps=_recreate(container, service, host or node.label),
        )
        for node, container, host, reference, running, tagged, service in _stated(estate, IMAGE_BEHIND)
    )


def images_untagged(estate: FindingEstate) -> tuple[Finding, ...]:
    """A container running an image no tag names, so nothing says what it is."""

    return distinct(
        Finding(
            rule="container-image-untagged",
            subject=node.id,
            title=f"{container} on {node.label} runs an untagged image",
            severity=Tone.ATTENTION,
            explanation="Its version cannot be told or reproduced.",
            part=container,
            evidence=(
                ("Container", container_link(node.label, container)),
                ("Image", running),
                *((("Started from", reference),) if reference else ()),
            ),
            steps=(Do("Pin a tag in its compose file."), *_recreate(container, service, host or node.label)),
        )
        for node, container, host, reference, running, _tagged, service in _stated(estate, IMAGE_UNTAGGED)
    )


def _recreate(container: str, service: str, host: str) -> tuple[Run, ...]:
    """Recreate the compose service, run in the project's directory on the machine."""

    return (
        Run(
            label=f"On {host}, in the compose project that runs {container}, recreate it",
            command=f"docker compose up -d --force-recreate {service}".rstrip(),
        ),
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "unrecognized-container",
        "Container started outside compose",
        Tone.SERIOUS,
        unrecognized_containers,
        gathered=True,
        operator_action="Adopt it, or remove it on its machine.",
        no_help_reason="HQ cannot remove a container it did not start.",
        card=CONTAINERS_CARD,
        bars={Tone.SERIOUS: "a container runs that no compose project started"},
    ),
    FindingRule(
        "container-image-behind",
        "Container runs an older image than its tag",
        Tone.ATTENTION,
        images_behind,
        gathered=True,
        operator_action="Recreate it from its compose project.",
        no_help_reason=cannot_run_commands(),
        card=CONTAINERS_CARD,
        bars={Tone.ATTENTION: "a container runs an older image than its tag names now"},
    ),
    FindingRule(
        "container-image-untagged",
        "Container runs an untagged image",
        Tone.ATTENTION,
        images_untagged,
        gathered=True,
        operator_action="Pin a tag in its compose file, then recreate it.",
        no_help_reason=CANNOT_EDIT_COMPOSE,
        card=CONTAINERS_CARD,
        bars={Tone.ATTENTION: "a container runs an image with no tag"},
    ),
    FindingRule(
        IMAGES_UNUSED,
        "Unused images",
        Tone.NEUTRAL,
        images_unused,
        gathered=True,
        operator_action="Remove the images nothing runs from the machine.",
        no_help_reason=cannot_run_commands(),
        card=CONTAINERS_CARD,
        bars={Tone.NEUTRAL: "a machine holds images no container runs and HQ does not keep"},
    ),
)
