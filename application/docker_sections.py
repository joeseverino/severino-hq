"""The machine page's Docker bands, read from the Portainer readings.

Each band asks the join engine for one reading kind about the machine, so a
record reaches the page by the same keys it reaches the relation graph.
A record that only says why a part was unreadable is left to the facts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Callable

from control_plane.observations.portainer import (
    ENVIRONMENT_KIND,
    IMAGE_KIND,
    NETWORK_KIND,
    STACK_KIND,
    VOLUME_KIND,
    image_title,
    short_id,
)

from .docker_estate import IMAGE_BEHIND, image_verdicts
from .labels import human_bytes
from .entity_links import entity_link
from .facts import Subject, readings
from .service_context import Cell, ServiceSection
from .timestamps import moment
from .ui import MISSING, counted


def _records(machine, kind: str) -> tuple[Mapping[str, Any], ...]:
    subject = Subject.of(
        hostnames=(getattr(machine, "name", ""), *(getattr(machine, "aliases", ()) or ())),
        addresses=getattr(machine, "addresses", ()) or (),
    )
    if not subject:
        return ()
    return tuple(joined.record for joined in readings().about(subject, kinds=(kind,)))


def _text(value: Any) -> Cell:
    text = str(value or "")
    return Cell(text) if text else Cell(MISSING, muted=True)


def _names(values: Iterable[str]) -> Cell:
    names = [str(value) for value in values or () if value]
    return Cell(", ".join(names)) if names else Cell("none", muted=True)


def _environments(machine) -> ServiceSection | None:
    records = _records(machine, ENVIRONMENT_KIND)
    if not records:
        return None
    return ServiceSection(
        id="docker-environment",
        renders=(ENVIRONMENT_KIND,),
        label="Docker environment",
        columns=("Environment", "Type", "Status", "Agent", "Docker", "Containers", "Read through"),
        records=tuple(
            (
                Cell(str(record.get("name", ""))),
                _text(record.get("type")),
                _text(record.get("status")),
                _text(record.get("agent_version")),
                _text(record.get("docker_version")),
                _text(
                    f"{record['containers_running']} of {record['containers_total']} running"
                    if record.get("containers_total") is not None
                    and record.get("containers_running") is not None
                    else ""
                ),
                Cell.of(entity_link("connection", str(record.get("connection_ref", ""))))
                if record.get("connection_ref")
                else Cell(MISSING, muted=True),
            )
            for record in records
        ),
        compact=True,
    )


def _networks(machine) -> ServiceSection | None:
    records = sorted(_records(machine, NETWORK_KIND), key=lambda record: str(record.get("name", "")))
    if not records:
        return None
    return ServiceSection(
        id="docker-networks",
        renders=(NETWORK_KIND,),
        folded=True,
        label="Networks",
        columns=("Network", "Driver", "Subnet", "Containers on it"),
        records=tuple(
            (
                Cell(str(record.get("name", ""))),
                _text(record.get("driver")),
                _names(record.get("subnets")),
                _names(record.get("containers")),
            )
            for record in records
        ),
    )


def _users(record: Mapping[str, Any]) -> Cell:
    users = [
        f"{user.get('container', '')} at {user.get('destination', '')}"
        + (" (read only)" if user.get("read_only") else "")
        for user in record.get("used_by") or ()
    ]
    return Cell("; ".join(users)) if users else Cell("unused", muted=True)


def _data(machine) -> ServiceSection | None:
    records = sorted(
        _records(machine, VOLUME_KIND),
        key=lambda record: (record.get("type") != "volume", str(record.get("name") or record.get("source"))),
    )
    if not records:
        return None
    return ServiceSection(
        id="docker-data",
        renders=(VOLUME_KIND,),
        folded=True,
        label="Where data lives",
        columns=("Mount", "Kind", "On the machine", "Used by", "Project"),
        records=tuple(
            (
                Cell(str(record.get("name") or record.get("source") or "")),
                Cell("Named volume" if record.get("type") == "volume" else "Host path"),
                _text(record.get("source")),
                _users(record),
                _text(record.get("stack")),
            )
            for record in records
        ),
    )


def unused_images(machine) -> tuple[Mapping[str, Any], ...]:
    """Images on the machine no container runs: what a prune would reclaim."""

    return tuple(record for record in _records(machine, IMAGE_KIND) if not record.get("containers"))


def _image_state(verdicts: Mapping[str, str], record: Mapping[str, Any]) -> Cell | None:
    """What needs doing about an image, or None when nothing does."""

    if not record.get("containers"):
        return Cell("Unused", muted=True)
    if verdicts.get(str(record.get("id", ""))) == IMAGE_BEHIND:
        # Pulled, and not yet what runs: recreating the container runs it.
        return Cell("Behind its tag")
    if not record.get("tags"):
        return Cell("Untagged")
    return None


def _images(machine) -> ServiceSection | None:
    """Only images that need something. One a container runs at its tag is the
    containers table's to show, with whether it is current."""

    records = sorted(_records(machine, IMAGE_KIND), key=image_title)
    verdicts = {running: fact for fact, _c, _r, running, _t, _s in image_verdicts(records)}
    rows = [(record, state) for record in records if (state := _image_state(verdicts, record)) is not None]
    if not rows:
        return None
    return ServiceSection(
        id="docker-images",
        # Every image, because the containers table shows those it runs.
        renders=(IMAGE_KIND,),
        label="Images",
        columns=("Image", "Id", "Created", "Size", "State"),
        records=tuple(
            (
                Cell(image_title(record)),
                Cell(short_id(str(record.get("id", "")))),
                _text(created.date().isoformat() if (created := moment(record.get("created_at"))) else ""),
                _text(human_bytes(record["size"]) if isinstance(record.get("size"), int) else ""),
                state,
            )
            for record, state in rows
        ),
        compact=True,
    )


def _projects(machine) -> ServiceSection | None:
    records = sorted(_records(machine, STACK_KIND), key=lambda record: str(record.get("name", "")))
    if not records:
        return None
    return ServiceSection(
        id="docker-projects",
        renders=(STACK_KIND,),
        folded=True,
        label="Compose projects",
        columns=("Project", "Started by", "Directory", "Containers"),
        records=tuple(
            (
                Cell(str(record.get("name", ""))),
                Cell(
                    "Portainer"
                    + (f", {record['status']}" if record.get("status") else "")
                    if record.get("source") == "portainer"
                    else "Compose on the machine"
                ),
                _text(record.get("working_dir")),
                Cell(counted(len(record.get("containers") or ()), "container"))
                if record.get("containers")
                else Cell("none running", muted=True),
            )
            for record in records
        ),
    )


SECTIONS: tuple[Callable[[object], ServiceSection | None], ...] = (
    _environments,
    _projects,
    _networks,
    _data,
    _images,
)
