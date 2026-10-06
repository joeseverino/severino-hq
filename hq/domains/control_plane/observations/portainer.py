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

from .contract import ObservationRecord, ObservationSpec, container_key

PROVIDER = "portainer"
# The API key acts as its user; the user must be granted each environment.
ENVIRONMENT_ACCESS = "environment access"

ENVIRONMENT_KIND = "portainer.environment"
NETWORK_KIND = "portainer.network"
VOLUME_KIND = "portainer.volume"
IMAGE_KIND = "portainer.image"
STACK_KIND = "portainer.compose_project"
RUNTIME_KIND = "portainer.runtime"

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


class RuntimeMount(ObservationRecord):
    type: str = ""
    # A host path for a bind, a volume's name for a volume.
    source: str = ""
    destination: str = ""
    read_only: bool = False


class PortBinding(ObservationRecord):
    container_port: str = ""
    host_ip: str = ""
    host_port: str = ""


class RuntimeRecord(_OnMachine):
    """How one container is run, from Docker's inspect of it.

    Only what decides what the container can reach or do on its machine. The
    inspect document also carries the environment, the command line and every
    label, where secrets live, and none of those is named here, so none can be
    stored.
    """

    container: str
    stack: str = ""
    service: str = ""
    image_id: str = ""
    # As the image or compose sets it: blank or "0"/"root" is root.
    user: str = ""
    privileged: bool = False
    read_only_rootfs: bool = False
    network_mode: str = ""
    pid_mode: str = ""
    ipc_mode: str = ""
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    # "no-new-privileges:true", "seccomp=unconfined": the option, never a profile body.
    security_opt: tuple[str, ...] = ()
    # Host device paths passed through.
    devices: tuple[str, ...] = ()
    mounts: tuple[RuntimeMount, ...] = ()
    port_bindings: tuple[PortBinding, ...] = ()
    # The ports the image says it listens on ("80/tcp" read as 80).
    exposed_ports: tuple[int, ...] = ()
    # Bytes and CPUs; zero is no limit.
    memory_limit: int = 0
    cpu_limit: float = 0.0
    pids_limit: int = 0
    restart_policy: str = ""
    # Whether the image or compose declares a health check, and its verdict.
    healthcheck: bool = False
    health: str = ""
    restart_count: int = 0
    started_at: str = ""


def _machine(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(value for value in (str(record.get("host_address", "") or ""),) if value)


def _host(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(value for value in (str(record.get("host", "") or ""),) if value)


def _users(field: str, of=lambda item: item):
    """The containers a record names, each keyed on the record's machine."""

    def keys(record: Mapping[str, Any]) -> tuple[str, ...]:
        named = record.get(field)
        items = (named,) if isinstance(named, str) else (named or ())
        return tuple(key for key in (container_key(record.get("host"), of(item)) for item in items) if key)

    return keys


def _network(record: Mapping[str, Any]) -> str:
    parts = [str(record.get("driver", "") or ""), *(str(subnet) for subnet in record.get("subnets") or ())]
    if record.get("internal"):
        parts.append("internal, no route out")
    return " · ".join(part for part in parts if part)


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
        relation="Docker",
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
        relation="Networks",
        describe=_network,
        containers=_users("containers"),
        container_relation="On network",
        connects=lambda record: str(record.get("name", "")) not in DEFAULT_NETWORKS,
    ),
    ObservationSpec(
        VOLUME_KIND,
        PROVIDER,
        "Volumes and mounts",
        VolumeRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=_volume_title,
        relation="Stores data in",
        containers=_users("used_by", lambda user: user.get("container")),
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
        relation="Images",
        containers=_users("containers", lambda user: user.get("container")),
        container_relation="Runs image",
    ),
    ObservationSpec(
        RUNTIME_KIND,
        PROVIDER,
        "Container settings",
        RuntimeRecord,
        requires=(ENVIRONMENT_ACCESS,),
        hostnames=_host,
        addresses=_machine,
        title=lambda record: str(record.get("container", "")),
        relation="Runs",
        containers=_users("container"),
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
        relation="Started by compose project",
        containers=_users("containers"),
        container_relation="Started by",
    ),
)
