"""The Docker readings joined into the relation graph, and what they imply.

A container running an image its own tag no longer names
locally, or an image with no tag at all, is a fact on its machine, and a
finding reads it from there, as it reads a container no compose project
declares. Nothing here calls a registry: "behind" means behind the image
already pulled onto the machine.
"""

import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import Any

from hq.domains.control_plane.observations.portainer import IMAGE_KIND, short_id
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from .facts import inventory_records
from .finding_model import (
    CANNOT_EDIT_COMPOSE,
    FindingRule,
    built_findings,
    cannot_run_commands,
    machine_step,
)

IMAGE_BEHIND = "image-behind"
IMAGE_UNTAGGED = "image-untagged"


def add(nodes, edges, resources, machine: Callable[[Any], str]) -> None:
    """Container facts on machines."""

    _image_facts(nodes, machine)
    _unrecognised(nodes, machine)


def _add_fact(nodes, node_id: str, fact: tuple[str, str]) -> None:
    node = nodes[node_id]
    if fact not in node.facts:
        nodes[node_id] = replace(node, facts=(*node.facts, fact))


def _unrecognised(nodes, machine) -> None:
    """A container on a machine that no compose project declares, as its fact.

    The sweep did not adopt it, so it has no node of its own; its machine
    carries it, and a finding reads it from there.
    """

    from .adoption import unmanaged

    for item in unmanaged():
        if item.adoptable or item.kind != CONTAINER_KIND:
            continue
        host = str(item.spec.get("host", ""))
        machine_id = machine(host)
        if machine_id:
            _add_fact(nodes, machine_id, ("unrecognised-container", f"{item.spec.get('name', '')}@{host}"))


def _on(record: Mapping[str, Any], machine: Callable[[Any], str]) -> str:
    return machine(record.get("host")) or machine(record.get("host_address"))


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


def _image_facts(nodes, machine) -> None:
    by_environment: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for _snapshot, record in inventory_records(IMAGE_KIND):
        if record.get("id"):
            key = (str(record.get("connection_ref", "")), record.get("environment_id"))
            by_environment.setdefault(key, []).append(record)
    for records in by_environment.values():
        host_id = _on(records[0], machine)
        if not host_id or host_id not in nodes:
            continue
        host = str(records[0].get("host", "") or nodes[host_id].label)
        added = tuple(
            (
                fact,
                "|".join(
                    (f"{container}@{host}", reference, short_id(running), short_id(tagged), service)
                ),
            )
            for fact, container, reference, running, tagged, service in image_verdicts(records)
        )
        for fact in added:
            _add_fact(nodes, host_id, fact)


# ----- Findings ------------------------------------------------------------------


def _parts(value: str) -> tuple[str, ...]:
    """``(container, host, reference, running, tagged, service)`` from one image fact."""

    subject, reference, running, tagged, service = (value.split("|") + [""] * 5)[:5]
    container, _, host = subject.rpartition("@")
    return container, host, reference, running, tagged, service


def unrecognised_containers(estate: Any) -> tuple[dict[str, Any], ...]:
    """A container a sweep found that no compose project declares.

    Every container that belongs on a machine is created by a compose project,
    and HQ takes those on by itself. Anything else was started by hand or by
    something HQ does not know, so it is reported rather than adopted: a
    container nobody declared must never look like one somebody did.
    """

    from django.urls import NoReverseMatch

    from hq.platform.application.routes import reverse

    from .finding_model import Remedy
    from .inventory import record_token

    found: list[dict[str, Any]] = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != "unrecognised-container":
                continue
            name, _, host = value.rpartition("@")
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
                {
                    "rule": "unrecognised-container",
                    "subject": node.id,
                    "title": f"Unrecognised container {name} on {node.label}",
                    "severity": "serious",
                    "explanation": (
                        "No compose project started it, so HQ is not tracking it. "
                        "Adopt it if you started it. Otherwise find what did and remove it."
                    ),
                    "steps": machine_step(
                        f"If nothing needs it, remove it from {host or node.label}",
                        host or node.label,
                        f"docker rm -f {shlex.quote(name)}",
                    ),
                    "evidence": (("Container", name), ("Machine", node.label)),
                    "remedies": (
                        Remedy(
                            capability="infrastructure.resource.create",
                            target=name,
                            label="Adopt it",
                            effect="HQ starts watching it like any other container.",
                            url=adopt_url,
                            method="POST",
                        ),
                    ),
                }
            )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


def images_behind(estate: Any) -> tuple[dict[str, Any], ...]:
    """A container still running the image its tag named before the last pull."""

    found = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != IMAGE_BEHIND:
                continue
            container, host, reference, running, tagged, service = _parts(value)
            found.append(
                {
                    "rule": "container-image-behind",
                    "subject": node.id,
                    "title": f"{container} on {node.label} runs an older {reference}",
                    "severity": "attention",
                    "explanation": (
                        f"{reference} on this machine is now image {tagged}, and the "
                        f"container still runs {running}. Recreate it to run what "
                        "was pulled."
                    ),
                    "evidence": (
                        ("Container", container),
                        ("Image reference", reference),
                        ("Running", running),
                        ("Tagged now", tagged),
                    ),
                    "steps": _recreate(container, service, host or node.label),
                }
            )
    return tuple(sorted(found, key=lambda item: item["title"]))


def images_untagged(estate: Any) -> tuple[dict[str, Any], ...]:
    """A container running an image no tag names, so nothing says what it is."""

    found = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != IMAGE_UNTAGGED:
                continue
            container, host, reference, running, _tagged, service = _parts(value)
            found.append(
                {
                    "rule": "container-image-untagged",
                    "subject": node.id,
                    "title": f"{container} on {node.label} runs an untagged image",
                    "severity": "attention",
                    "explanation": (
                        "No tag on this machine names the image it runs, so its "
                        "version cannot be told or reproduced. Pin a tag in its "
                        "compose file and recreate it."
                    ),
                    "evidence": (
                        ("Container", container),
                        ("Image", running),
                        *((("Started from", reference),) if reference else ()),
                    ),
                    "steps": _recreate(container, service, host or node.label),
                }
            )
    return tuple(sorted(found, key=lambda item: item["title"]))


def _step(**fields: Any) -> Any:
    from .finding_model import OperatorStep

    return OperatorStep(**fields)


def _recreate(container: str, service: str, host: str) -> tuple:
    """Recreate the compose service, run in the project's directory on the machine."""

    return (
        _step(
            label=f"On {host}, in the compose project that runs {container}, recreate it",
            command=f"docker compose up -d --force-recreate {service}".rstrip(),
        ),
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "unrecognised-container",
        "A container no compose project started",
        "serious",
        lambda estate: built_findings(unrecognised_containers(estate)),
        operator_action=(
            "Adopt it if you started it. Otherwise remove it on its machine."
        ),
        no_help_reason=(
            "HQ cannot remove a container it did not start."
        ),
    ),
    FindingRule(
        "container-image-behind",
        "Container runs an older image than its tag",
        "attention",
        lambda estate: built_findings(images_behind(estate)),
        operator_action=(
            "Recreate the container from its compose project so it runs the image its tag names now."
        ),
        no_help_reason=cannot_run_commands(),
    ),
    FindingRule(
        "container-image-untagged",
        "Container runs an untagged image",
        "attention",
        lambda estate: built_findings(images_untagged(estate)),
        operator_action=(
            "Pin a tag for the image in the container's compose file and recreate it."
        ),
        no_help_reason=CANNOT_EDIT_COMPOSE,
    ),
)
