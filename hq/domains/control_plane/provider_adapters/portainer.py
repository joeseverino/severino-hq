"""Portainer: the containers HQ watches, as declared.

The controller (controller/providers) reads and acts on them; what is declared
about them is here.
"""

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field, field_validator

from hq.platform.application.command_fields import RequiredText
from hq.platform.application.container_standard import CHECK_IDS
from hq.platform.application.github_public import GitHubRepositoryURL
from hq.platform.application.record_fields import one_of

from ..connection_shapes import API_TOKEN
from ..observations import portainer as readings
from ..provider_spec import (
    ConnectionKind,
    ProviderModel,
    ProviderSpec,
    applies,
    key_from,
    locked,
)

CONTAINER_KIND = "portainer.container"

# A check of the container standard, by id. The set is read at run time, so a
# type checker reads the key as text.
if TYPE_CHECKING:
    type CheckId = str
else:
    CheckId = one_of(sorted(CHECK_IDS))


class PortainerContainerSpec(ProviderModel):
    """One container HQ is responsible for keeping up, not for defining.

    Identity and nothing else: its definition lives in whatever compose file
    created it, which HQ does not see, so declaring one says "this is mine to
    watch and to cycle", and reconciliation is locked with nothing to converge.
    """

    connection_ref: RequiredText = Field(
        max_length=160,
        title="Portainer",
        description="The Portainer that manages this machine.",
    )
    host: RequiredText = Field(
        max_length=160,
        title="Runs on",
        description="The machine the container runs on.",
    )
    name: RequiredText = Field(
        max_length=200,
        title="Container",
        description="The container name, as Docker reports it.",
    )
    on_demand: bool = Field(
        default=False,
        title="Runs on demand",
        description=("Usually stopped and removed, so HQ does not flag it as missing."),
    )
    holds_docker_socket: bool = Field(
        default=False,
        title="Holds the Docker socket",
        description="A socket proxy or agent whose job is Docker. Its socket access is listed, not flagged.",
    )
    by_design: dict[CheckId, Annotated[str, Field(pattern=r"\S")]] = Field(
        default_factory=dict,
        title="Checks its job requires",
        description=(
            "Checks it fails on purpose, by id, each with the reason, such as DNS "
            "on the host network. HQ shows the reason instead of a problem."
        ),
    )
    hidden: bool = Field(
        default=False,
        title="Collapse on machine page",
        description=("Collapse it on the machine's page. HQ still watches and controls it."),
    )
    serves_ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(
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

    @field_validator("by_design")
    @classmethod
    def _reasons_trimmed(cls, value: dict[str, str]) -> dict[str, str]:
        return {check: reason.strip() for check, reason in value.items()}


def _container_readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
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


CONTAINER = ProviderSpec(
    CONTAINER_KIND,
    "A container HQ watches and can start, stop or restart. Its compose file still defines it.",
    PortainerContainerSpec,
    actions={
        "reconcile": locked("Saved in HQ. Its compose file does not change."),
        # Any container, even one Portainer did not create: these are
        # Docker's verbs, not Portainer's.
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
    _from_record=_container_from_record,
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
    advanced_fields=("hidden", "on_demand", "holds_docker_socket", "by_design", "serves_ports", "source"),
    # So a sweep can never confirm it: the field exists for the case Docker
    # publishes nothing.
    #
    # ``hidden`` is HQ's own bookkeeping (whether the machine page folds the
    # row away); Portainer and Docker have nowhere to keep it. ``source``
    # is the operator's word for an image that names no repository.
    unobservable_fields=("serves_ports", "hidden", "on_demand", "holds_docker_socket", "by_design", "source"),
    declaration_only=True,
    choices="hq.platform.application.provider_choices:container",
)


DEFINITIONS = (CONTAINER,)
OBSERVATIONS = readings.OBSERVATIONS

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {
    "portainer": ConnectionKind(
        "Portainer",
        "coarse",
        API_TOKEN,
        create="An access token in Portainer, under My account, Access tokens.",
        create_url="https://docs.portainer.io/api/access",
        gives="Every container on your machines, with its image, stack, volumes and networks.",
    )
}
