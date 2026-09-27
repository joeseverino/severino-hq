"""Portainer readings: environments, and each Docker environment's networks,
data mounts, images and compose projects, and the calls every Portainer read
makes through the controller's runtime.

One container list per environment feeds every reading here and the container
sweep beside them. An environment that cannot be read is the reading refused on
that machine; every environment refusing is the whole reading refused.
"""

from __future__ import annotations

import socket
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.network import parse_ip

from ..observations.portainer import (
    ENVIRONMENT_ACCESS,
    ENVIRONMENT_KIND,
    IMAGE_KIND,
    NETWORK_KIND,
    STACK_KIND,
    VOLUME_KIND,
)
from .contracts import ProviderRuntime
from .parts import refuse_part
from .refusals import refused

PROVIDER = "portainer"

# Portainer's EndpointType and EndpointStatus numbers.
ENVIRONMENT_TYPES = {
    1: "docker",
    2: "agent",
    3: "azure",
    4: "edge agent",
    5: "kubernetes",
    6: "kubernetes agent",
    7: "kubernetes edge agent",
}
ENVIRONMENT_STATUS = {1: "up", 2: "down"}
STACK_STATUS = {1: "active", 2: "inactive"}

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_WORKING_DIR = "com.docker.compose.project.working_dir"
COMPOSE_CONFIG_FILES = "com.docker.compose.project.config_files"
COMPOSE_SERVICE = "com.docker.compose.service"


# ----- Calls -------------------------------------------------------------------


def url(runtime: ProviderRuntime, ref: str = "") -> str:
    base = runtime.required(runtime.connection_prefix(PROVIDER, ref), "URL").rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"


def headers(runtime: ProviderRuntime, ref: str = "") -> dict[str, str]:
    return {"X-API-Key": runtime.required(runtime.connection_prefix(PROVIDER, ref), "API_TOKEN")}


def get(runtime: ProviderRuntime, ref: str, path: str) -> Any:
    return runtime.request(f"{url(runtime, ref)}{path}", headers=headers(runtime, ref))


def _an_address(host: str) -> str:
    """A name resolved to the address it answers at, or the name when it does not
    resolve: an address is what every other source of a machine reports."""

    if not host or parse_ip(host) is not None:
        return host
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return host


def load_environments(runtime: ProviderRuntime, ref: str = "") -> list[dict[str, Any]]:
    """Every Docker environment, with the machine each one is.

    Portainer names its own local environment `local`, which is nobody's
    hostname. An agent carries the machine's address in its URL, and a unix
    socket is the machine Portainer runs on, so it takes Portainer's address.
    """

    listed = get(runtime, ref, "/endpoints")
    portainer_at = _an_address(urllib.parse.urlsplit(url(runtime, ref)).hostname or "")
    resolved = []
    for raw in listed or []:
        at = str(raw.get("URL", ""))
        address = urllib.parse.urlsplit(at).hostname if "://" in at else ""
        resolved.append(environment(raw, address or "", portainer_at))
    return resolved


def environments(runtime: ProviderRuntime, ref: str = "") -> list[dict[str, Any]]:
    return runtime.snapshot_value(
        ("portainer-environments", ref), lambda: load_environments(runtime, ref)
    )


def stack_list(runtime: ProviderRuntime, ref: str = "") -> list[dict[str, Any]]:
    return runtime.snapshot_value(
        ("portainer-stacks", ref), lambda: get(runtime, ref, "/stacks") or []
    )


def docker(runtime: ProviderRuntime, ref: str, environment_id: int, path: str) -> Any:
    """One Docker API call through Portainer, shared across a sweep's readings."""

    return runtime.snapshot_value(
        ("portainer-docker", ref, environment_id, path),
        lambda: get(runtime, ref, f"/endpoints/{environment_id}/docker{path}"),
    )


@dataclass(frozen=True)
class PortainerReads:
    """The Portainer calls a reading makes."""

    refs: Callable[[], tuple[str, ...]]
    # Each environment as ``environment`` below describes it.
    environments: Callable[[str], list[dict[str, Any]]]
    # ``GET /endpoints/{id}/docker<path>`` for one connection.
    docker: Callable[[str, int, str], Any]
    # ``GET /stacks`` for one connection.
    stacks: Callable[[str], list[dict[str, Any]]]
    # The machine the controller runs on, which a local socket is.
    local_host: Callable[[], str]
    # Whether a container is this controller's own run, never reported.
    own_run: Callable[[Mapping[str, Any]], bool]

    @classmethod
    def through(cls, runtime: ProviderRuntime) -> "PortainerReads":
        return cls(
            refs=lambda: runtime.connection_refs(PROVIDER),
            environments=lambda ref: environments(runtime, ref),
            docker=lambda ref, environment_id, path: docker(runtime, ref, environment_id, path),
            stacks=lambda ref: stack_list(runtime, ref),
            local_host=runtime.controller_id,
            own_run=runtime.own_run,
        )


def _stamp(seconds: Any) -> str:
    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value > 0 else ""


def environment(raw: Mapping[str, Any], address: str, portainer_at: str) -> dict[str, Any]:
    """One environment from ``GET /endpoints``: identity, reach, agent and Docker."""

    snapshots = raw.get("Snapshots") or ()
    snapshot = snapshots[-1] if snapshots and isinstance(snapshots[-1], Mapping) else {}
    kind = raw.get("Type")
    return {
        "id": raw.get("Id"),
        "name": str(raw.get("Name", "") or ""),
        "address": address or portainer_at,
        "local": not address,
        "reachable": raw.get("Status") == 1,
        "type": ENVIRONMENT_TYPES.get(kind, str(kind or "")),
        "status": ENVIRONMENT_STATUS.get(raw.get("Status"), ""),
        "agent_version": str((raw.get("Agent") or {}).get("Version", "") or ""),
        "docker_version": str(snapshot.get("DockerVersion", "") or ""),
        "containers_running": snapshot.get("RunningContainerCount"),
        "containers_total": snapshot.get("ContainerCount"),
        "snapshot_at": _stamp(snapshot.get("Time")),
    }


def machine_name(found: Mapping[str, Any], local_host: str) -> str:
    """The name HQ files an environment's machine under.

    A local socket is the machine the controller runs on, which is the name
    the topology already uses for it.
    """

    return local_host if found.get("local") and local_host else str(found.get("name", ""))


# ----- Containers --------------------------------------------------------------


def published(container: dict[str, Any]) -> tuple[list[int], int | None, bool]:
    """Every port this answers on, the one that is unambiguous, and its reach.

    A published port carries the address it was bound to. Bound to the loopback
    it is reachable only from inside that machine, which is the difference
    between a proxy in a container reaching it and returning 502.

    The single port is named only when exactly one is published. A proxy
    publishing 80, 81 and 443 has no one port, and picking the first would print
    a guess beside facts.
    """

    ports: set[int] = set()
    reachable = True
    for port in container.get("Ports") or ():
        public = port.get("PublicPort")
        if not public:
            continue
        ports.add(int(public))
        if str(port.get("IP", "")) in {"127.0.0.1", "::1"}:
            reachable = False
    listed = sorted(ports)
    return listed, (listed[0] if len(listed) == 1 else None), reachable


def container_record(
    container: dict[str, Any],
    host: str,
    connection_ref: str,
    portainer_stacks: frozenset[str] = frozenset(),
    host_address: str = "",
) -> dict[str, Any]:
    """What Portainer knows about one container, in HQ's vocabulary."""

    labels = container.get("Labels") or {}
    ports, port, reachable = published(container)
    stack = labels.get("com.docker.compose.project", "")
    return {
        # Whether Portainer created this, or merely sees it. Everything running
        # today was started by compose on the machine, so Portainer holds no
        # stack for any of it, and a declaration built as though it did would
        # ask Portainer to stand up a second copy of something already serving.
        "portainer_managed": bool(stack) and stack in portainer_stacks,
        "name": (container.get("Names") or ["/"])[0].lstrip("/"),
        "stack": stack,
        "working_dir": labels.get("com.docker.compose.project.working_dir", ""),
        "image": container.get("Image", ""),
        # How it is attached, because it decides whether the ports below can
        # mean anything. A container on the host network binds the machine's
        # ports directly and Docker reports none for it, so an empty list is
        # "cannot be known from here" rather than "publishes nothing", and
        # only this field tells the two apart.
        "network_mode": (container.get("HostConfig") or {}).get("NetworkMode", ""),
        "state": container.get("State", ""),
        "status": container.get("Status", ""),
        "ports": ports,
        "port": port,
        "reachable": reachable,
        "host": host,
        # Where the machine is, not just what this credential calls it. Two
        # credentials name one machine differently (an SSH item and a
        # Portainer environment for the same VPS) and the address is the only
        # thing both agree on. Without it HQ lists one machine twice and files
        # its containers under whichever name the sweep used.
        "host_address": host_address,
        "connection_ref": connection_ref,
    }


# ----- Environments ------------------------------------------------------------


def read_environments(api: PortainerReads) -> list[dict[str, Any]]:
    """Every environment each Portainer connection reaches, up or down."""

    local = api.local_host()
    found = []
    for ref in api.refs():
        for item in _listed(api.environments, ref, "The environment list"):
            found.append(
                {
                    "connection_ref": ref,
                    "host": machine_name(item, local),
                    **{
                        key: item.get(key)
                        for key in (
                            "id",
                            "name",
                            "address",
                            "local",
                            "type",
                            "status",
                            "agent_version",
                            "docker_version",
                            "containers_running",
                            "containers_total",
                            "snapshot_at",
                        )
                    },
                }
            )
    return found


def _listed(call: Callable[[str], Any], ref: str, what: str) -> list[Any]:
    try:
        return list(call(ref) or ())
    except Exception as exc:  # noqa: BLE001 - classified and re-raised
        raise refused(exc, what=what, needs=ENVIRONMENT_ACCESS) from exc


# ----- What each Docker environment holds ---------------------------------------


@dataclass(frozen=True)
class _Environment:
    ref: str
    id: int
    host: str
    address: str

    @property
    def base(self) -> dict[str, Any]:
        return {
            "connection_ref": self.ref,
            "environment_id": self.id,
            "host": self.host,
            "host_address": self.address,
        }


def _environments(api: PortainerReads) -> Iterator[_Environment]:
    local = api.local_host()
    for ref in api.refs():
        for item in _listed(api.environments, ref, "The environment list"):
            if item.get("reachable"):
                yield _Environment(ref, item["id"], machine_name(item, local), str(item.get("address", "")))


def _containers(api: PortainerReads, at: _Environment) -> list[dict[str, Any]]:
    return [
        item
        for item in api.docker(at.ref, at.id, "/containers/json?all=1") or ()
        if isinstance(item, dict) and not api.own_run(item)
    ]


def _name(container: Mapping[str, Any]) -> str:
    return str((container.get("Names") or ["/"])[0]).lstrip("/")


def _each(
    api: PortainerReads, what: str, build: Callable[[PortainerReads, _Environment], Iterable[dict]]
) -> list[dict[str, Any]]:
    """Records from every reachable environment.

    An environment that cannot be read is the reading refused on its machine;
    every environment refusing is a refused read and raises.
    """

    found: list[dict[str, Any]] = []
    failures: list[Exception] = []
    reachable = list(_environments(api))
    for at in reachable:
        try:
            found.extend({**at.base, **record} for record in build(api, at))
        except Exception as exc:  # noqa: BLE001 - classified, reported per environment
            error = refused(exc, what=what, needs=ENVIRONMENT_ACCESS)
            failures.append(error)
            refuse_part("", error, scope=at.host, connection_ref=at.ref, address=at.address)
    if reachable and len(failures) == len(reachable):
        raise failures[0]
    return found


def _networks(api: PortainerReads, at: _Environment) -> Iterator[dict[str, Any]]:
    attached: dict[str, list[str]] = {}
    for container in _containers(api, at):
        networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
        for name in networks:
            attached.setdefault(str(name), []).append(_name(container))
    for network in api.docker(at.ref, at.id, "/networks") or ():
        name = str(network.get("Name", "") or "")
        if not name:
            continue
        ipam = (network.get("IPAM") or {}).get("Config") or ()
        yield {
            "id": str(network.get("Id", "") or ""),
            "name": name,
            "driver": str(network.get("Driver", "") or ""),
            "scope": str(network.get("Scope", "") or ""),
            "internal": bool(network.get("Internal")),
            "subnets": tuple(
                str(entry.get("Subnet")) for entry in ipam if isinstance(entry, dict) and entry.get("Subnet")
            ),
            "containers": tuple(sorted(attached.get(name, ()))),
        }


def _mount_users(containers: list[dict[str, Any]], kind: str) -> dict[str, list[dict[str, Any]]]:
    """Each mount of ``kind`` by its name (a volume) or source (a bind), and who mounts it."""

    users: dict[str, list[dict[str, Any]]] = {}
    for container in containers:
        for mount in container.get("Mounts") or ():
            if mount.get("Type") != kind:
                continue
            key = str(mount.get("Name") if kind == "volume" else mount.get("Source") or "")
            if key:
                users.setdefault(key, []).append(
                    {
                        "container": _name(container),
                        "destination": str(mount.get("Destination", "") or ""),
                        "read_only": mount.get("RW") is False,
                    }
                )
    return users


def _volumes(api: PortainerReads, at: _Environment) -> Iterator[dict[str, Any]]:
    containers = _containers(api, at)
    named = _mount_users(containers, "volume")
    listed = (api.docker(at.ref, at.id, "/volumes") or {}).get("Volumes") or ()
    for volume in listed:
        name = str(volume.get("Name", "") or "")
        if not name:
            continue
        yield {
            "type": "volume",
            "name": name,
            "driver": str(volume.get("Driver", "") or ""),
            "source": str(volume.get("Mountpoint", "") or ""),
            "stack": str((volume.get("Labels") or {}).get(COMPOSE_PROJECT, "") or ""),
            "created_at": str(volume.get("CreatedAt", "") or ""),
            "used_by": tuple(named.get(name, ())),
        }
    for source, users in sorted(_mount_users(containers, "bind").items()):
        yield {"type": "bind", "source": source, "used_by": tuple(users)}


def _images(api: PortainerReads, at: _Environment) -> Iterator[dict[str, Any]]:
    running: dict[str, list[dict[str, str]]] = {}
    for container in _containers(api, at):
        image_id = str(container.get("ImageID", "") or "")
        if image_id:
            running.setdefault(image_id, []).append(
                {
                    "container": _name(container),
                    "reference": str(container.get("Image", "") or ""),
                    "service": str((container.get("Labels") or {}).get(COMPOSE_SERVICE, "") or ""),
                }
            )
    for image in api.docker(at.ref, at.id, "/images/json") or ():
        image_id = str(image.get("Id", "") or "")
        if not image_id:
            continue
        yield {
            "id": image_id,
            "tags": tuple(tag for tag in image.get("RepoTags") or () if tag and tag != "<none>:<none>"),
            "digests": tuple(
                digest for digest in image.get("RepoDigests") or () if digest and "<none>" not in digest
            ),
            "created_at": _stamp(image.get("Created")),
            "size": image.get("Size") if isinstance(image.get("Size"), int) else None,
            "containers": tuple(running.get(image_id, ())),
        }


def _stacks(api: PortainerReads, at: _Environment) -> Iterator[dict[str, Any]]:
    projects: dict[str, dict[str, Any]] = {}
    for container in _containers(api, at):
        labels = container.get("Labels") or {}
        name = str(labels.get(COMPOSE_PROJECT, "") or "")
        if not name:
            continue
        project = projects.setdefault(
            name,
            {
                "name": name,
                "source": "compose",
                "working_dir": str(labels.get(COMPOSE_WORKING_DIR, "") or ""),
                "config_files": tuple(
                    part.strip()
                    for part in str(labels.get(COMPOSE_CONFIG_FILES, "") or "").split(",")
                    if part.strip()
                ),
                "containers": [],
            },
        )
        project["containers"].append(_name(container))
    for stack in api.stacks(at.ref) or ():
        if stack.get("EndpointId") != at.id or not stack.get("Name"):
            continue
        project = projects.setdefault(str(stack["Name"]), {"name": str(stack["Name"]), "containers": []})
        project.update(
            source="portainer",
            status=STACK_STATUS.get(stack.get("Status"), ""),
            entry_point=str(stack.get("EntryPoint", "") or ""),
        )
        project.setdefault("working_dir", str(stack.get("ProjectPath", "") or ""))
    for project in sorted(projects.values(), key=lambda item: item["name"]):
        yield {**project, "containers": tuple(sorted(project["containers"]))}


def _reading(read: Callable[[PortainerReads], list[dict[str, Any]]]):
    return lambda runtime: read(PortainerReads.through(runtime))


def _each_in(what: str, build: Callable[[PortainerReads, _Environment], Iterable[dict]]):
    return _reading(lambda api: _each(api, what, build))


# The readings this module answers; the Portainer adapter declares them.
READINGS = {
    ENVIRONMENT_KIND: _reading(read_environments),
    NETWORK_KIND: _each_in("The network list", _networks),
    VOLUME_KIND: _each_in("The volume list", _volumes),
    IMAGE_KIND: _each_in("The image list", _images),
    STACK_KIND: _each_in("The stack list", _stacks),
}
