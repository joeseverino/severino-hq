from unittest import mock

from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource

from ...exposure import OPEN, PRIVATE
from ...tone import Tone
from ..test_container_standard import KEPT, runtime
from ..test_containers import HIGH, estate, exposed, inventory, running
from . import Example, Examples, Raised

PROXY = "example/socket-proxy"
DIGEST, OTHER = "sha256:" + "e" * 64, "sha256:" + "f" * 64
SOCKET = {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}
# Nothing routes to it, and HQ read everything that could.
ADOPT = "infrastructure.resource.create"
UNROUTED = mock.patch("hq.platform.application.paths.reads_every_route", return_value=True)


def box(*, reach=None, runtimes=(), declared: dict | None = None, raises=(), **said) -> Example:
    """example-box's containers (``test_containers.estate``): app behind a
    release, kuma behind its tag, web current; each reached as ``reach``
    says, its ``runtimes`` read, and the containers ``declared`` watched."""

    def given():
        estate(**said)
        if runtimes:
            inventory("portainer.runtime", list(runtimes))
        for name, spec in (declared or {}).items():
            ManagedResource.objects.create(
                key=f"example-box-{name}",
                kind="portainer.container",
                spec={"connection_ref": "example-portainer", "host": "example-box", "name": name, **spec},
            )
        return exposed(reach) if isinstance(reach, str) else reach

    return Example(given=given, raises=raises)


def started(reference: str, *digests: str, registry: dict | None = None, raises=()) -> Example:
    """proxy on example-box, started from ``reference``, its image held under ``digests``."""

    def given():
        inventory("portainer.container", [running("proxy", reference)])
        image = {"connection_ref": "example-portainer", "host": "example-box", "id": DIGEST}
        user = {"container": "proxy", "reference": reference, "service": "proxy"}
        inventory(
            "portainer.image", [{**image, "tags": [f"{PROXY}:1.13.1"], "digests": list(digests), "containers": [user]}]
        )
        read = registry or {"tags": ["1.13.1"]}
        inventory("registry.image", [{"image": f"docker.io/{PROXY}", "read_at": timezone.now().isoformat(), **read}])

    return Example(given=given, raises=raises)


def pinned(title: str, pin: str) -> Raised:
    return Raised(title=title, evidence=(("Container", "proxy"), ("Pin it as", pin)))


ADVISED = "ghcr.io/example/app:v1.2.0 has 1 known vulnerability"
# proxy, started from the newest tag its registry holds, by digest.
NEWEST = started(f"{PROXY}:1.13.1@{DIGEST}", f"{PROXY}@{DIGEST}")

EXAMPLES = (
    Examples(
        "container-image-advisory",
        fires=(
            # As urgent as what reaches it: where HQ does not know what reaches it, as if anyone may.
            box(advisories=[HIGH], raises=(Raised(title=ADVISED, severity=Tone.SERIOUS),)),
            box(advisories=[HIGH], reach=OPEN, raises=(Raised(title=ADVISED, severity=Tone.SERIOUS),)),
            box(advisories=[HIGH], reach=PRIVATE, raises=(ADVISED,)),
            box(advisories=[HIGH], reach=UNROUTED, raises=(Raised(title=ADVISED, severity=Tone.NEUTRAL),)),
            # Many advisories are one thing to do.
            box(
                advisories=[HIGH, {**HIGH, "id": "GHSA-high-2"}, {**HIGH, "id": "GHSA-high-3"}],
                reach=OPEN,
                raises=(Raised(title="ghcr.io/example/app:v1.2.0 has 3 known vulnerabilities", severity=Tone.SERIOUS),),
            ),
            # One no release fixes yet is said, not urged.
            box(
                advisories=[HIGH],
                app_tags=("v1.2.0",),
                reach=OPEN,
                raises=(f"{ADVISED}, and no release fixes it yet",),
            ),
        ),
        quiet=(box(),),
    ),
    Examples(
        "container-image-update",
        fires=(box(raises=("example/kuma:1 has an update", "ghcr.io/example/app:v1.2.0 has an update")),),
        quiet=(NEWEST,),
    ),
    Examples(
        "container-image-unpinned",
        fires=(
            started(
                f"{PROXY}:1.13.1",
                f"{PROXY}@{DIGEST}",
                raises=(pinned(f"{PROXY}:1.13.1 runs by tag alone", f"{PROXY}:1.13.1@{DIGEST}"),),
            ),
            # Docker Hub's short name and its full one are the same image.
            started(
                f"docker.io/{PROXY}:1.13.1",
                f"example/other@{OTHER}",
                f"{PROXY}@{DIGEST}",
                raises=(pinned(f"docker.io/{PROXY}:1.13.1 runs by tag alone", f"docker.io/{PROXY}:1.13.1@{DIGEST}"),),
            ),
        ),
        quiet=(
            NEWEST,
            # Pulled under another name, there is nothing to pin to.
            started(f"{PROXY}:1.13.1", f"example/other@{OTHER}"),
            # A registry that does not answer for it is never asked to pin it.
            started(f"{PROXY}:1.13.1", f"{PROXY}@{DIGEST}", registry={"unread": "the registry returned HTTP 401."}),
        ),
    ),
    Examples(
        "container-hardening",
        fires=(
            # One finding per container, of its serious checks alone.
            box(
                runtimes=(runtime("web", privileged=True, user=""), runtime("kuma", privileged=True, user="")),
                raises=(
                    Raised(title="kuma on example-box: runs privileged", presses=(ADOPT,)),
                    Raised(title="web on example-box: runs privileged", presses=(ADOPT,)),
                ),
            ),
            # The socket is marked where the container is watched, else adopted.
            box(
                runtimes=(
                    runtime("web", **{**KEPT, "mounts": [SOCKET]}),
                    runtime("kuma", **{**KEPT, "mounts": [SOCKET]}),
                ),
                declared={"web": {}},
                raises=(
                    Raised(title="kuma on example-box: holds the Docker socket", presses=(ADOPT,)),
                    Raised(
                        title="web on example-box: holds the Docker socket", presses=("infrastructure.resource.update",)
                    ),
                ),
            ),
        ),
        # One declared to hold the socket.
        quiet=(
            box(),
            box(
                runtimes=(runtime("web", **{**KEPT, "mounts": [SOCKET]}),),
                declared={"web": {"holds_docker_socket": True}},
            ),
        ),
    ),
)
