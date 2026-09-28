"""Portainer: stacks, containers and the controller's own run."""

from __future__ import annotations

import os
from typing import Any

from control_plane.providers import controller_id
from control_plane.provider_adapters.contracts import (
    ProviderError,
    ProviderResult,
)
from control_plane.provider_adapters import portainer_readings
from control_plane.provider_adapters.portainer import CONTAINER_KIND, CONTAINER_STACK_KIND
from .handlers import acts, lists, probes
from . import connection_env, provider_http, provider_runtime


# --- Portainer ---------------------------------------------------------------
#
# Portainer holds one credential and reaches every Docker host registered with
# it, so a machine becomes available to HQ by being an environment there rather
# than by anything here naming it.


def portainer_url(connection_ref: str = "") -> str:
    return portainer_readings.url(provider_runtime.RUNTIME, connection_ref)


def portainer_headers(connection_ref: str = "") -> dict[str, str]:
    return portainer_readings.headers(provider_runtime.RUNTIME, connection_ref)


def load_portainer_environments(connection_ref: str = "") -> list[dict[str, Any]]:
    return portainer_readings.load_environments(provider_runtime.RUNTIME, connection_ref)


def _portainer_environments(connection_ref: str = "") -> list[dict[str, Any]]:
    return portainer_readings.environments(provider_runtime.RUNTIME, connection_ref)


def _portainer_environment_for(host: str, connection_ref: str = "") -> dict[str, Any]:
    """The environment that is a given machine.

    Matched on address, or on the environment's own name, or (for the local
    socket) on the machine Portainer runs on, which the controller knows
    because it is the machine it runs on too.
    """

    environments = _portainer_environments(connection_ref)
    for environment in environments:
        if environment["address"] and environment["address"] == host:
            return environment
        if environment["name"] == host:
            return environment
    local_host = controller_id()
    for environment in environments:
        if environment["local"] and local_host and local_host == host:
            return environment
    raise ProviderError(f"No Portainer environment is {host!r}.")


def _portainer_stack_list(connection_ref: str = "") -> list[dict[str, Any]]:
    return portainer_readings.stack_list(provider_runtime.RUNTIME, connection_ref)


def _portainer_stacks(
    environment_id: int, connection_ref: str = ""
) -> list[dict[str, Any]]:
    return [
        stack
        for stack in _portainer_stack_list(connection_ref)
        if stack.get("EndpointId") == environment_id
    ]


def _portainer_docker(connection_ref: str, environment_id: int, path: str) -> Any:
    return portainer_readings.docker(provider_runtime.RUNTIME, connection_ref, environment_id, path)


def _portainer_containers(
    environment_id: int, connection_ref: str = ""
) -> list[dict[str, Any]]:
    return _portainer_docker(connection_ref, environment_id, "/containers/json?all=1") or []


def _stack_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "Name": spec["name"],
        "StackFileContent": spec["compose"],
        "Env": [
            {"name": item.get("name", ""), "value": item.get("value", "")}
            for item in spec.get("environment") or ()
        ],
    }


@acts(CONTAINER_STACK_KIND, "reconcile")
def reconcile_portainer(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    if not environment["reachable"]:
        raise ProviderError(f"Portainer cannot currently reach {spec['host']}.")
    existing = [
        stack
        for stack in _portainer_stacks(environment["id"], connection_ref)
        if stack.get("Name") == spec["name"]
    ]
    if len(existing) > 1:
        raise ProviderError("Portainer holds more than one stack of that name.")

    changed = True
    if existing:
        stack = existing[0]
        current = provider_http.request_json(
            f"{portainer_url(connection_ref)}/stacks/{stack['Id']}/file",
            headers=portainer_headers(connection_ref),
        )
        if (current or {}).get("StackFileContent") == spec["compose"]:
            changed = False
        elif apply:
            provider_http.request_json(
                f"{portainer_url(connection_ref)}/stacks/{stack['Id']}"
                f"?endpointId={environment['id']}",
                method="PUT",
                headers=portainer_headers(connection_ref),
                payload={**_stack_payload(spec), "PullImage": False},
            )
    elif apply:
        provider_http.request_json(
            f"{portainer_url(connection_ref)}/stacks/create/standalone/string"
            f"?endpointId={environment['id']}",
            method="POST",
            headers=portainer_headers(connection_ref),
            payload=_stack_payload(spec),
        )

    # What is actually running, which is the only thing worth reporting: a
    # stack Portainer accepted and Docker then failed to start is not Ready.
    containers = [
        portainer_readings.container_record(container, spec["host"], connection_ref)
        for container in _portainer_containers(environment["id"], connection_ref)
        if (container.get("Labels") or {}).get("com.docker.compose.project")
        == spec["name"]
    ]
    running = [item for item in containers if item["state"] == "running"]
    unreachable = [item for item in containers if not item["reachable"]]
    status = {
        "environment": environment["name"],
        "host": spec["host"],
        "containers": containers,
        "origin": f"{spec['host']}:{spec['port']}" if spec.get("port") else "",
        "state": "running" if running and len(running) == len(containers) else "",
    }

    if apply and not containers:
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                provider_http.condition(
                    "Degraded",
                    True,
                    "NotRunning",
                    "The stack exists in Portainer but no container from it is "
                    "running.",
                )
            ],
            message="Stack is declared but nothing is running.",
        )
    if unreachable:
        names = ", ".join(sorted(item["name"] for item in unreachable))
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                provider_http.condition(
                    "Degraded",
                    True,
                    "BoundToLoopback",
                    f"{names} publishes a port on the loopback address, so "
                    "nothing outside that machine can reach it, including a "
                    "proxy running in a container on the same host.",
                )
            ],
            message="Stack is running but is not reachable.",
        )
    return ProviderResult(
        changed=changed,
        status=status,
        conditions=[provider_http.condition("Ready", True, "Reconciled", "Stack is running.")],
        message="Stack updated." if changed else "Stack unchanged.",
    )


@acts(CONTAINER_STACK_KIND, "delete")
def delete_portainer(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    existing = [
        stack
        for stack in _portainer_stacks(environment["id"], connection_ref)
        if stack.get("Name") == spec["name"]
    ]
    if not existing:
        return ProviderResult(
            changed=False,
            status={},
            conditions=[provider_http.condition("Ready", True, "Absent", "Stack is already gone.")],
            message="Stack was already absent.",
        )
    if apply:
        provider_http.request_json(
            f"{portainer_url(connection_ref)}/stacks/{existing[0]['Id']}"
            f"?endpointId={environment['id']}",
            method="DELETE",
            headers=portainer_headers(connection_ref),
        )
    return ProviderResult(
        changed=True,
        status={},
        conditions=[provider_http.condition("Ready", True, "Deleted", "Stack removed.")],
        message="Stack removed.",
    )


RUN_LABEL = "severino-hq.run"


def is_this_run(container: dict[str, Any]) -> bool:
    """Whether a container is the controller running this sweep.

    Matched on a per-run nonce the launcher sets as both a label and
    ``HQ_CONTROLLER_RUN``. Any container can carry any label, so a fixed label
    would let one hide itself from the sweep; a nonce it cannot know does not.
    """

    nonce = os.environ.get("HQ_CONTROLLER_RUN", "").strip()
    labels = container.get("Labels") or {}
    return bool(nonce) and labels.get(RUN_LABEL) == nonce


def _list_portainer_containers() -> list[dict[str, Any]]:
    """Every container Portainer can see, on every machine it reaches.

    Containers rather than stacks, and reported as such. A stack listing
    describes only what Portainer itself created, and everything standing up
    today was started by compose on the machine, so a stack listing reports an
    estate of nothing while ten containers run.

    The distinction is not pedantic. Docker will start, stop and restart any
    container; Portainer will only do so for a stack it made. Modelling what is
    running as a container is what lets HQ cycle one it did not create.
    """

    local_host = controller_id()
    records: list[dict[str, Any]] = []
    for connection_ref in connection_env.provider_connection_refs("portainer"):
        for environment in _portainer_environments(connection_ref):
            if not environment["reachable"]:
                continue
            host = portainer_readings.machine_name(environment, local_host)
            created_here = frozenset(
                str(stack.get("Name", ""))
                for stack in _portainer_stacks(environment["id"], connection_ref)
                if stack.get("Name")
            )
            for container in _portainer_containers(environment["id"], connection_ref):
                if is_this_run(container):
                    continue
                records.append(
                    portainer_readings.container_record(
                        container,
                        host,
                        connection_ref,
                        created_here,
                        environment["address"],
                    )
                )
    return records


@lists(CONTAINER_KIND)
def list_portainer_containers() -> list[dict[str, Any]]:
    return provider_http.snapshot_value(("portainer-containers",), _list_portainer_containers)


def _portainer_container_id(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The Docker id of a declared container, and the environment holding it.

    Looked up by name on each pass rather than stored. A container id changes
    every time it is recreated, so a stored one would address a container that
    is gone.
    """

    connection_ref = spec.get("connection_ref", "")
    environment = _portainer_environment_for(spec["host"], connection_ref)
    wanted = spec["name"]
    for container in _portainer_containers(environment["id"], connection_ref):
        names = [str(name).lstrip("/") for name in container.get("Names") or ()]
        if wanted in names:
            return str(container.get("Id", "")), environment
    raise ProviderError(f"No container named {wanted!r} on {spec['host']}.")


def _cycle_portainer_container(
    spec: dict[str, Any], verb: str, *, apply: bool
) -> ProviderResult:
    """Start, stop or restart one container, and report what it did.

    Docker answers these with 204 on success and 304 when the container is
    already in the state asked for, so "start an already-running container" is
    not an error and must not be reported as one.
    """

    connection_ref = spec.get("connection_ref", "")
    container_id, environment = _portainer_container_id(spec)
    if apply:
        provider_http.request_json(
            f"{portainer_url(connection_ref)}/endpoints/{environment['id']}"
            f"/docker/containers/{container_id}/{verb}",
            method="POST",
            headers=portainer_headers(connection_ref),
            payload={},
        )
    # Read back rather than trusting the call. A restart that brought the
    # container up and let it exit two seconds later reports success at the API
    # and is not what was asked for.
    observed = [
        portainer_readings.container_record(container, spec["host"], connection_ref)
        for container in _portainer_containers(environment["id"], connection_ref)
        if spec["name"]
        in [str(name).lstrip("/") for name in container.get("Names") or ()]
    ]
    state = observed[0]["state"] if observed else ""
    status = {
        "host": spec["host"],
        "container": spec["name"],
        "state": state,
        "containers": observed,
    }
    settled = state == ("exited" if verb == "stop" else "running")
    return ProviderResult(
        changed=apply,
        status=status,
        conditions=[
            provider_http.condition(
                "Ready" if settled else "Degraded",
                True,
                verb.capitalize() + ("ed" if verb == "stop" else "ed"),
                f"{spec['name']} is {state or 'in an unknown state'}.",
            )
        ],
        message=f"{spec['name']} is {state or 'in an unknown state'}.",
    )


@acts(CONTAINER_KIND, "restart")
def restart_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "restart", apply=apply)


@acts(CONTAINER_KIND, "start")
def start_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "start", apply=apply)


@acts(CONTAINER_KIND, "stop")
def stop_portainer_container(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    return _cycle_portainer_container(spec, "stop", apply=apply)


@probes("portainer")
def _probe_portainer(connection_ref: str) -> dict[str, Any]:
    environments = _portainer_environments(connection_ref)
    reachable = [item for item in environments if item["reachable"]]
    local_host = controller_id()
    return {
        "detail": (f"{len(reachable)} of {len(environments)} environments reachable."),
        "reaches": sorted(
            local_host if item["local"] and local_host else item["name"]
            for item in reachable
        ),
    }
