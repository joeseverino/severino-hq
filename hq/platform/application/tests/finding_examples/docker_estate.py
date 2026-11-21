from unittest import mock

from hq.platform.core.testing import declare

from ...finding_steps import Run
from . import Example, Examples, Raised, connection

ON_LAB = {"connection_ref": "example-portainer", "environment_id": 1, "host": "lab-1", "host_address": "192.0.2.10"}
OLD, NEW, LOOSE = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64


def lab() -> None:
    """lab-1, whose containers example-portainer reads, and which HQ manages through it."""

    connection("example-portainer", "portainer", manages=True)
    declare("lab-1", "machine", {"name": "lab-1", "addresses": ["192.0.2.10"]})


def container(name: str, stack: str = "") -> dict:
    return {"name": name, "host": "lab-1", "connection_ref": "example-portainer", "stack": stack}


def image(id_: str, *tags: str, running: str = "", reference: str = "") -> dict:
    containers = [{"container": running, "reference": reference, "service": running}] if running else []
    return {**ON_LAB, "id": id_, "tags": list(tags), "containers": containers}


def images(*records: dict, raises: tuple[Raised | str, ...] = ()) -> Example:
    return Example(readings={"portainer.image": records}, given=lab, raises=raises)


# The worker's image, which the release pins and nothing runs between its runs.
WORKER = {**image(LOOSE), "digests": [f"ghcr.io/example/worker@{LOOSE}"], "size": 1_000_000}


def kept(*records: dict, raises: tuple[Raised | str, ...] = ()) -> Example:
    """``records`` on lab-1, with the worker's image one HQ keeps."""

    def given():
        lab()
        return mock.patch(
            "hq.platform.application.docker_estate.release_pins", return_value=(f"ghcr.io/example/worker@{LOOSE}",)
        )

    return Example(readings={"portainer.image": records}, given=given, raises=raises)


# web runs the image example/web:1 named before the last pull; job runs one no tag names.
BEHIND = (image(OLD, running="web", reference="example/web:1"), image(NEW, "example/web:1"))
UNTAGGED = (image(LOOSE, running="job", reference=LOOSE),)
IN_USE = image(NEW, "example/web:1", running="web", reference="example/web:1")
CURRENT = images(IN_USE)

EXAMPLES = (
    Examples(
        "unrecognized-container",
        # The container its compose project declares is not reported.
        fires=(
            Example(
                readings={"portainer.container": (container("a-web", "a-project"), container("a-stray"))},
                given=lab,
                raises=(
                    Raised(
                        title="Unrecognized container a-stray on lab-1", presses=("infrastructure.resource.create",)
                    ),
                ),
            ),
        ),
        quiet=(Example(readings={"portainer.container": (container("a-web", "a-project"),)}, given=lab),),
    ),
    Examples(
        "container-image-behind",
        fires=(images(*BEHIND, *UNTAGGED, raises=("web on lab-1 runs an older example/web:1",)),),
        quiet=(CURRENT,),
    ),
    Examples(
        "container-image-untagged",
        fires=(images(*BEHIND, *UNTAGGED, raises=("job on lab-1 runs an untagged image",)),),
        quiet=(CURRENT,),
    ),
    Examples(
        "container-images-unused",
        # Removed by name, so the step leaves what HQ keeps and what runs.
        fires=(
            kept(
                IN_USE,
                WORKER,
                {**image(OLD, "example/web:0"), "size": 2_000_000},
                raises=(
                    Raised(
                        title="1 unused image on lab-1",
                        evidence=(("Image", "example/web:0"),),
                        steps=(
                            Run(
                                label="Remove them from lab-1", command='ssh lab-1 "sudo docker image rm example/web:0"'
                            ),
                        ),
                    ),
                ),
            ),
        ),
        quiet=(CURRENT, kept(IN_USE, WORKER)),
    ),
)
