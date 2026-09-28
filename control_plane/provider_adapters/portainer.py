"""Portainer: container stacks HQ runs, containers it watches, and what it reads.

The kinds' actions, inventory and connection probe are still the controller
core's; what is declared about them, and the readings, are here.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from application.github_public import GitHubRepositoryURL

from ..provider_spec import NameContext, ProviderModel, ProviderSpec, applies, key_from, locked
from .contracts import ControllerIntegrationAdapter
from .portainer_readings import PROVIDER, READINGS


CONTAINER_KIND = "portainer.container"
CONTAINER_STACK_KIND = "portainer.stack"


class PortainerContainerSpec(ProviderModel):
    """One container HQ is responsible for keeping up, not for defining.

    Deliberately identity and nothing else. A container's definition lives in
    whatever compose file created it, which HQ has never seen and must not
    pretend to own: declaring one here says "this is mine to watch and to
    cycle", and reconciliation is locked because there is nothing to converge.

    That is what makes it usable at all. Almost nothing running was created by
    Portainer, so almost nothing can be declared as a stack; every container can
    be started, stopped and restarted, because those are Docker's verbs rather
    than Portainer's.
    """

    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Portainer",
        description="The Portainer that manages this machine.",
    )
    host: str = Field(
        min_length=1,
        max_length=160,
        title="Runs on",
        description="The machine this runs on.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        title="Container",
        description="The container name, as Docker reports it.",
    )
    on_demand: bool = Field(
        default=False,
        title="Runs on demand",
        description=(
            "Usually stopped and removed. A sweep that misses it is not a "
            "finding."
        ),
    )
    holds_docker_socket: bool = Field(default=False, title="Holds the Docker socket", description="Its job is the socket: a socket proxy or an agent. Listed, never an action item.")
    hidden: bool = Field(
        default=False,
        title="Collapse on machine page",
        description=(
            "Collapse it on the machine's page. HQ still watches and controls "
            "it."
        ),
    )
    serves_ports: list[int] = Field(
        default_factory=list,
        title="Answers on",
        description=(
            "Host-network containers only. Docker reports no ports for them, "
            "so list the ports here to link a proxy to it."
        ),
    )
    source: GitHubRepositoryURL = Field(
        default="",
        max_length=300,
        title="Built from",
        description=(
            "The GitHub repository its image is built from, for an image that "
            "does not say. HQ reads its releases and advisories."
        ),
    )

    @field_validator("serves_ports")
    @classmethod
    def ports_are_ports(cls, value: list[int]) -> list[int]:
        if any(port < 1 or port > 65535 for port in value):
            raise ValueError("Ports must be between 1 and 65535.")
        return value


class PortainerStackEnvVar(ProviderModel):
    name: str = Field(min_length=1, max_length=200, title="Name")
    value: str = Field(default="", max_length=4000, title="Value")


class PortainerStackSpec(ProviderModel):
    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Portainer",
        description="The Portainer environment this runs in.",
    )
    host: str = Field(
        min_length=1,
        max_length=160,
        title="Runs on",
        description="The machine this runs on.",
    )
    name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        title="Stack name",
        description="Lowercase and hyphenated. Used as the compose project name.",
    )
    compose: str = Field(
        min_length=1,
        title="Compose file",
        description="The docker compose file, as it would be on disk.",
    )
    environment: list[PortainerStackEnvVar] = Field(
        default_factory=list,
        title="Environment",
        description="Values the compose file reads. Keep secrets in 1Password.",
    )
    hostnames: list[str] = Field(
        default_factory=list,
        title="Serves",
        description="Hostnames that reach this stack from outside, if any.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        title="Answers on port",
        description=(
            "The port on the machine. Needed only for host-network containers. "
            "Published ports are read from the running container."
        ),
    )


def _stack_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec.get("hostnames") or ())


def _stack_origin(spec: dict[str, Any]) -> str:
    """Where this answers, as the topology names the machine.

    ``_locate`` matches a host by id as readily as by address, so a stack says
    which machine it runs on and never repeats that machine's address. A stack
    with no port answers nothing directly (it is reached through whatever
    fronts it) and returning nothing is the honest form of that.
    """

    port = spec.get("port")
    host = spec.get("host", "")
    return f"{host}:{port}" if host and port else ""


def _stack_seed(context: NameContext) -> dict[str, Any]:
    """A stack seeded from the name it will serve.

    The name doubles as the stack's own, lowercased and hyphenated the way
    compose projects are, so publishing a service does not ask for it twice.
    """

    label = key_from(context.hostname)
    return {"hostnames": [context.hostname], "name": label or "service"}


def _stack_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    port = spec.get("port")
    where = f"{spec.get('host', '')}:{port}" if port else spec.get("host", "")
    return (
        ("Runs on", where, status.get("origin", "")),
        ("Stack", spec.get("name", ""), status.get("state", "")),
    )


def _stack_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A declaration matching a container the controller already reported.

    Adopting takes what is running rather than asking for it again: the stack
    name, the machine, and the published port when the container has one. A
    container on the host network publishes nothing, so its port stays for the
    operator to supply: nothing else knows it.
    """

    return {
        "name": record.get("stack", ""),
        "host": record.get("host", ""),
        "port": record.get("port") or None,
        "compose": record.get("compose", ""),
        "hostnames": list(record.get("hostnames") or ()),
        "connection_ref": record.get("connection_ref", ""),
    }


def _container_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    # No "Runs on" row: the card carries the machine as a link, and printing it
    # here as text would say it twice in the same box.
    #
    # No "State" row either: what a container is doing comes from the sweep and
    # is on the card above, with its uptime.
    return (("Container", spec.get("name", ""), status.get("container", "")),)


def _container_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_ref": record.get("connection_ref", ""),
        "host": record.get("host", ""),
        "name": record.get("name", ""),
    }


def _container_adopts(record: dict[str, Any]) -> bool:
    """A container a compose project created is declared in its compose file.
    One started by hand is declared nowhere, so it waits for a person."""

    return bool(record.get("stack"))


def _container_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec.get("host", ""), spec.get("name", ""))


def _container_key_hint(spec: dict[str, Any]) -> str:
    return key_from(f"{spec.get('host', '')}-{spec.get('name', '')}")


def _container_removal_note(spec: dict[str, Any]) -> str:
    return (
        f"HQ stops watching {spec.get('name', 'this container')} and can no "
        "longer start, stop or restart it. The container keeps running."
    )


STACK = ProviderSpec(
    CONTAINER_STACK_KIND,
    "A set of containers on one machine. HQ creates it in Portainer if "
    "it does not exist.",
    PortainerStackSpec,
    actions={
        "reconcile": applies(automatic=True),
        "delete": applies(),
    },
    label="Container stack",
    connection_providers=("portainer",),
    # Which Portainer is folded away with the tuning. There is normally one,
    # the menu selects it, and asking first makes the form open on the
    # question an operator is least likely to have an opinion about.
    advanced_fields=("connection_ref", "environment"),
    removal_note=lambda spec: (
        f"{spec.get('name', 'This stack')} stops running on "
        f"{spec.get('host', 'its machine')}. Anything it serves goes "
        "offline."
    ),
    facet="runtime",
    hostnames=_stack_hostnames,
    origin=_stack_origin,
    seed=_stack_seed,
    readout=_stack_readout,
    from_record=_stack_from_record,
    sample_record={
        "stack": "example-stack",
        "host": "example-host",
        "connection_ref": "example-portainer",
        "compose": "services:\\n  web:\\n    image: example/web:1\\n",
        "hostnames": ["shop.example.com"],
        "port": 3000,
    },
    choices="application.provider_choices:container_stack",
    unobserved_reason=(
        "Observed through its containers, which the sweep reads."
    ),
)

CONTAINER = ProviderSpec(
    CONTAINER_KIND,
    "A container HQ watches and can start, stop or restart. Its compose "
    "file still defines it.",
    PortainerContainerSpec,
    actions={
        "reconcile": locked(
            "Defined by a compose file outside HQ. HQ can start, stop and restart it."
        ),
        "restart": applies(),
        "start": applies(),
        "stop": applies(),
    },
    label="Container",
    connection_providers=("portainer",),
    # No facet, no hostnames and no seed, so it is never offered as a way
    # to publish a name. A container answers wherever its ports are pointed
    # and the declaration does not say where that is; inventing a hostname
    # from a container name would put a service on the board that no name
    # reaches. It is adopted from what a sweep found, which is the only
    # place its identity is known.
    readout=_container_readout,
    from_record=_container_from_record,
    adopts=_container_adopts,
    sample_record={
        "name": "example-web",
        "host": "example-host",
        "connection_ref": "example-portainer",
        "stack": "example-stack",
    },
    identity=_container_identity,
    key_hint=_container_key_hint,
    removal_note=_container_removal_note,
    # Ports are behind the disclosure because the answer is usually none:
    # Docker reports them, and only a container sharing the machine's
    # network has to be told.
    advanced_fields=("hidden", "on_demand", "holds_docker_socket", "serves_ports", "source"),
    # So a sweep can never confirm it: the field exists for the case Docker
    # publishes nothing.
    #
    # ``hidden`` is HQ's own bookkeeping (whether the machine page folds the
    # row away); Portainer and Docker have nowhere to keep it. ``source``
    # is the operator's word for an image that names no repository.
    unobservable_fields=("serves_ports", "hidden", "on_demand", "holds_docker_socket", "source"),
    declaration_only=True,
    choices="application.provider_choices:container_stack",
)


# Declarations only: the controller half is still the core's, so the readings
# go through the connection these declare.
DEFINITIONS = (STACK, CONTAINER)

ADAPTER = ControllerIntegrationAdapter(
    definitions=(),
    inventory={},
    connection_probes={},
    actions={},
    readings=READINGS,
    reads_through=(PROVIDER,),
)
