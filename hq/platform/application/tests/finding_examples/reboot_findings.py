from datetime import timedelta

from ...finding_steps import Run
from ...freshness import stale_after
from . import NOW, Ago, Example, Examples, ago, connection, stored

KIND = "host.reboot"
EDGE_KIND = "host.edge_reboot"
# Past how long a reading of it stands.
OUT_OF_DATE = stale_after(KIND) + timedelta(minutes=10)


AT_ELEVEN = "Jan\xa015, 11:00\xa0AM"


def on(machine: str, command: str) -> str:
    return f'ssh {machine} "sudo {command}"'


def what_waits(machine: str) -> Run:
    return Run("See what waits on the restart", on(machine, "cat /run/reboot-required.pkgs"))


def restarted(machine: str = "example-controller") -> tuple[Run, ...]:
    return (
        what_waits(machine),
        Run(
            "Schedule the restart for a quiet hour",
            on(machine, "shutdown -r 03:00"),
            ("HQ reads the time back and shows it here.",),
        ),
    )


NEWER = "example-edge waits on a restart"


def reading(*, pending: bool = True, **said) -> dict:
    """The machine's restart state: a kernel and a library installed two days ago, nothing scheduled."""

    waiting = {"since": ago(days=2), "packages": ["linux-image-generic", "libc6"]} if pending else {}
    return {"pending": pending, **waiting, "read_at": NOW, **said}


def edge() -> None:
    """An SSH transport, which is the machine at its other end."""

    connection("example-edge", "ssh", endpoint="198.51.100.7:22", probed=False)


def on_its_own_edge(older: str, newer: str) -> Example:
    """A controller on the edge it reports as, both readings saying it waits, each read at its own time."""

    def said(kind: str, package: str) -> dict:
        return reading(packages=[package], **({"connection_ref": "example-edge"} if kind == EDGE_KIND else {}))

    return Example(
        readings={
            older: stored(said(older, "linux-image-older"), age=timedelta(hours=1), controller_id="example-edge"),
            newer: stored(said(newer, "linux-image-newer"), controller_id="example-edge"),
        },
        given=edge,
        raises=(NEWER,),
    )


EXAMPLES = (
    Examples(
        "reboot-pending",
        fires=(
            # Waiting since when and for what, and how to read then schedule the restart.
            Example(readings={KIND: (reading(),)}, raises=("example-controller waits on a restart",)),
            # A scheduled restart says when, and shows it rather than scheduling another.
            Example(
                readings={KIND: (reading(scheduled_action="reboot", scheduled_at=ago(hours=-5)),)},
                raises=(f"example-controller waits on a restart, scheduled for {AT_ELEVEN}",),
            ),
            # A restart scheduled with nothing waiting is said too.
            Example(
                readings={KIND: (reading(pending=False, scheduled_action="reboot", scheduled_at=ago(hours=-5)),)},
                raises=(f"example-controller is scheduled to restart at {AT_ELEVEN}",),
            ),
            # An edge that waits is its own machine's, with its steps run there.
            Example(
                readings={
                    KIND: (reading(pending=False),),
                    EDGE_KIND: (reading(connection_ref="example-edge", packages=["linux-image-example"]),),
                },
                given=edge,
                raises=("example-edge waits on a restart",),
            ),
            # A controller on its own edge waits once, as its newer reading says.
            on_its_own_edge(KIND, EDGE_KIND),
            on_its_own_edge(EDGE_KIND, KIND),
        ),
        quiet=(
            Example(readings={KIND: (reading(pending=False),)}),
            # A reading out of date is withheld.
            Example(readings={KIND: stored(reading(read_at=Ago(OUT_OF_DATE)), age=OUT_OF_DATE)}),
        ),
    ),
)
