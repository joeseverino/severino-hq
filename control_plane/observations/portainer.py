"""Readings from the Portainer credential: each Docker environment and what it holds.

Every record names the machine it was read on twice: ``host`` as the sweep
files containers, and ``host_address`` as every other source knows it. Both
are join keys, so a record reaches the machine page whichever one the machine
catalogue holds.

Environment variables, labels other than compose's own, and registry
credentials are never named, so the schema drops them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contract import ObservationRecord, ObservationSpec

PROVIDER = "portainer"
# The API key acts as its user; the user must be granted each environment.
ENVIRONMENT_ACCESS = "environment access"

ENVIRONMENT_KIND = "portainer.environment"
NETWORK_KIND = "portainer.network"
VOLUME_KIND = "portainer.volume"
IMAGE_KIND = "portainer.image"
STACK_KIND = "portainer.compose_project"

# Networks every Docker host has; sharing one says nothing about intent.
DEFAULT_NETWORKS = frozenset({"bridge", "host", "none"})


class _OnMachine(ObservationRecord):
    """A record read on one environment. An environment that could not be read
    is the whole reading refused on that machine, never a record."""

    connection_ref: str
    environment_id: int | None = None
    host: str = ""
    host_address: str = ""


class EnvironmentRecord(ObservationRecord):
    connection_ref: str
    id: int
    name: str = ""
    host: str = ""
    address: str = ""
    local: bool = False
    # "docker", "agent", "edge agent", "kubernetes", or the number Portainer reported.
    type: str = ""
    # "up" or "down".
    status: str = ""
    agent_version: str = ""
    docker_version: str = ""
    containers_running: int | None = None
    containers_total: int | None = None
    snapshot_at: str = ""


class NetworkRecord(_OnMachine):
    id: str = ""
    name: str = ""
    driver: str = ""
    scope: str = ""
    internal: bool = False
    subnets: tuple[str, ...] = ()
    # Names of the containers attached, from the container list.
    containers: tuple[str, ...] = ()


class MountUser(ObservationRecord):
    container: str
    destination: str = ""
    read_only: bool = False


class VolumeRecord(_OnMachine):
    # "volume" for a named volume, "bind" for a host path.
    type: str = "volume"
    name: str = ""
    # A named volume's driver and where Docker keeps it; a bind mount's host path.
    driver: str = ""
    source: str = ""
    # The compose project that created a named volume.
    stack: str = ""
    created_at: str = ""
    used_by: tuple[MountUser, ...] = ()


class ImageUser(ObservationRecord):
    container: str
    # The reference the container was started from: a tag, or an id.
    reference: str = ""
    # Its compose service, where compose started it.
    service: str = ""


class ImageRecord(_OnMachine):
    id: str = ""
    tags: tuple[str, ...] = ()
    digests: tuple[str, ...] = ()
    created_at: str = ""
    size: int | None = None
    containers: tuple[ImageUser, ...] = ()


class StackRecord(_OnMachine):
    name: str = ""
    # "portainer" for a stack Portainer deployed, "compose" for a project
    # found only through its containers' labels.
    source: str = "compose"
    # "active" or "inactive" for a Portainer stack.
    status: str = ""
    working_dir: str = ""
    config_files: tuple[str, ...] = ()
    entry_point: str = ""
    containers: tuple[str, ...] = ()


def _machine(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(value for value in (str(record.get("host_address", "") or ""),) if value)


def _host(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(value for value in (str(record.get("host", "") or ""),) if value)


def image_title(record: Mapping[str, Any]) -> str:
    tags = record.get("tags") or ()
    return str(tags[0]) if tags else short_id(str(record.get("id", "")))


def short_id(value: str) -> str:
    """A Docker id as Docker prints it: twelve characters, no algorithm."""

    return value.split(":", 1)[-1][:12]


def _volume_title(record: Mapping[str, Any]) -> str:
    return str(record.get("name") or record.get("source") or "")


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        ENVIRONMENT_KIND,
        PROVIDER,
        "Docker environment",
        EnvironmentRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=lambda record: tuple(
            value for value in (str(record.get("address", "") or ""),) if value
        ),
        title=lambda record: str(record.get("name", "")),
        relation="Docker environment",
    ),
    ObservationSpec(
        NETWORK_KIND,
        PROVIDER,
        "Docker network",
        NetworkRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=lambda record: str(record.get("name", "")),
        relation="Docker network",
    ),
    ObservationSpec(
        VOLUME_KIND,
        PROVIDER,
        "Data mount",
        VolumeRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=_volume_title,
        relation="Holds data in",
    ),
    ObservationSpec(
        IMAGE_KIND,
        PROVIDER,
        "Docker image",
        ImageRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=image_title,
        relation="Docker image",
    ),
    ObservationSpec(
        STACK_KIND,
        PROVIDER,
        "Compose project",
        StackRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=lambda record: str(record.get("name", "")),
        relation="Compose project",
    ),
)
