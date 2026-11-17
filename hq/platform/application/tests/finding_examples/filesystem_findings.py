from ...tone import Tone
from . import NOW, Example, Examples, Raised

KIND = "host.filesystem"
GIB = 1 << 30
MIB = 1 << 20


def filesystem(mount: str = "/", free: int = 10 * GIB, total: int = 29 * GIB) -> dict:
    return {"mount": mount, "free_bytes": free, "total_bytes": total, "read_at": NOW}


EXAMPLES = (
    Examples(
        "filesystem-nearly-full",
        fires=(
            # A root with megabytes left is serious; a /boot with most of it free is fine.
            Example(
                readings={KIND: (filesystem(free=59 * 1000 * 1000), filesystem("/boot", free=800 * MIB, total=GIB))},
                raises=("The filesystem at / on example-controller is nearly full",),
            ),
            # Under two gigabytes and a tenth is attention.
            Example(
                readings={KIND: (filesystem(free=int(1.5 * GIB)),)},
                raises=(
                    Raised(title="The filesystem at / on example-controller is nearly full", severity=Tone.ATTENTION),
                ),
            ),
            # A small partition nearly full is attention.
            Example(
                readings={KIND: (filesystem("/boot", free=50 * MIB, total=GIB),)},
                raises=(
                    Raised(
                        title="The filesystem at /boot on example-controller is nearly full", severity=Tone.ATTENTION
                    ),
                ),
            ),
            # Each full filesystem is named in one finding.
            Example(
                readings={
                    KIND: (filesystem(free=300 * MIB), filesystem("/var/lib/example", free=GIB, total=100 * GIB))
                },
                raises=("2 filesystems are nearly full on example-controller",),
            ),
        ),
        quiet=(
            # A small /boot under the floor but mostly free, and a large disk
            # under a tenth free with far more than the floor left.
            Example(
                readings={
                    KIND: (
                        filesystem(),
                        filesystem("/boot", free=800 * MIB, total=GIB),
                        filesystem("/srv", free=300 * GIB, total=4096 * GIB),
                    )
                }
            ),
        ),
    ),
)
