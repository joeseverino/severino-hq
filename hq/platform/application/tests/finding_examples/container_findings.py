from contextlib import ExitStack
from unittest import mock

from django.utils import timezone

from hq.platform.core.testing import declare

from ...exposure import OPEN, PRIVATE
from ...finding_steps import Do
from ...machines import Machine
from ...tone import Tone
from ..test_app_update_approval import applies
from ..test_app_updates import arranged, moved, pull
from ..test_container_standard import KEPT, runtime
from ..test_containers import HIGH, estate, exposed, inventory, running
from ..test_github_estate import store
from ..test_upgrades import TARGET
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
            declare(
                f"example-box-{name}",
                "portainer.container",
                {"connection_ref": "example-portainer", "host": "example-box", "name": name, **spec},
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
    return Raised(title=title, evidence=(("Container", "proxy"), ("Machine", "example-box"), ("Pin it as", pin)))


ADVISED = "ghcr.io/example/app:v1.2.0 has 1 known vulnerability"
# The target, v1.3.0, is still affected by one; affected by the other alone.
UNFIXED = {**HIGH, "id": "GHSA-unfixed", "vulnerabilities": [["< 2.0.0", ""]]}
BROUGHT = {**HIGH, "id": "GHSA-brought", "vulnerabilities": [[">= 1.3.0", ""]]}
SET_APP = Do(
    "On example-box, set the image of app where it is defined, then start it again.",
    value="image: ghcr.io/example/app:v1.3.0",
)
PINNED = f"ghcr.io/example/app:v1.2.1@{TARGET}"
PLANNED, LISTED = "/infrastructure/example-box-app/#upgrade", "/infrastructure/containers/"
# proxy, started from the newest tag its registry holds, by digest.
NEWEST = started(f"{PROXY}:1.13.1@{DIGEST}", f"{PROXY}@{DIGEST}")


def held(why: str, primary=("Open containers", LISTED), **said) -> Example:
    """A vulnerable app the internet reaches with no move worth making: said,
    not urged, and no step makes one."""

    steps = (Do("Move it to a release that fixes it."),)
    return box(reach=OPEN, raises=(Raised(title=f"{ADVISED}, and {why}", primary=primary, steps=steps),), **said)


BOX, EDGE = "example-box", "example-edge"
APPROVE, APPLY, OPEN_PR = "app_update.approve", "app_update.apply", "app_update.open"
KUMA = "kuma has an update, 1 to 2"
# What the pull request and the default branch pin: one release past what runs.
MOVED = "app has an update, v1.2.0 to v1.2.1"
REPOSITORY = {"compose": ["compose/app/compose.yaml"]}
# The digest the new pin names, as its publisher attached nothing to it.
UNLISTED = {"digest": f"ghcr.io/example/app@{TARGET}", "image": "ghcr.io/example/app", "packages": [], "sbom": ""}


def applied_on(*hosts: str) -> ExitStack:
    """The one adopted playbook that applies an app update, written for
    ``hosts``, among the machines HQ knows."""

    stack = ExitStack()
    stack.enter_context(
        mock.patch("hq.platform.application.app_update_approval.applier", return_value=applies(hosts=hosts))
    )
    machines = (Machine(BOX), Machine(EDGE))
    stack.enter_context(mock.patch("hq.platform.application.automation_runs.machine_catalog", return_value=machines))
    return stack


def update(*pulls, on: tuple[str, ...] | None = (BOX,), raises, scanned=(TARGET,), digests=(), **held) -> Example:
    """example-box's app, held in example/alpha with ``pulls`` open and the
    repository's ``held`` fields, applied by a playbook written for ``on``
    (None for no playbook); its new pin's packages ``scanned``, and what its
    publisher attached to each digest in ``digests``."""

    def given():
        arranged(*pulls, scanned=scanned, advisories=())
        if held:
            store(**{**REPOSITORY, **held})
        if digests:
            inventory("registry.digest", list(digests))
        return applied_on(*on) if on is not None else None

    return Example(given=given, raises=(*raises, KUMA))


def facts(pulled, brings) -> tuple[tuple[str, str], ...]:
    return (("Container", "app"), ("Machine", BOX), *pulled, brings)


# The pull request, and what it fixes of what runs.
PULLED = (("Pull request", "example/alpha#7"), ("Fixes", "None known"))


# An app a repository holds, in each state its update can stand in.
STATES = (
    # An open pull request is approved in one press, which previews, merges and applies it.
    update(
        pull(moves=[moved()]),
        raises=(
            Raised(
                title=MOVED,
                presses=(APPROVE,),
                evidence=facts(PULLED, ("New vulnerabilities", "Nothing serious")),
            ),
        ),
    ),
    # Its new pin not checked yet, or never checkable: its publisher attached no package list.
    update(
        pull(moves=[moved()]),
        scanned=(),
        raises=(
            Raised(
                title=MOVED,
                presses=(APPROVE,),
                evidence=facts(PULLED, ("New vulnerabilities", "Not checked yet")),
            ),
        ),
    ),
    update(
        pull(moves=[moved()]),
        scanned=(),
        digests=(UNLISTED,),
        raises=(
            Raised(
                title=MOVED,
                presses=(APPROVE,),
                evidence=facts(
                    PULLED, ("New vulnerabilities", "Can't be checked: example/app publishes no package list")
                ),
            ),
        ),
    ),
    # No press where no playbook applies it, or where the one that does never runs where it runs.
    update(pull(moves=[moved()]), on=None, raises=(MOVED,)),
    update(pull(moves=[moved()]), on=(EDGE,), raises=(MOVED,)),
    # Merged on the default branch and not applied: one press applies it at that head.
    update(
        compose_images=[{"path": "compose/app/compose.yaml", "service": "app", "image": PINNED}],
        raises=(Raised(title=MOVED, presses=(APPLY,)),),
    ),
    # Renovate holds its branch, or has not seen the release: one press has it open the pull request.
    # Where HQ can't match the repository's files to what runs, it says so.
    update(
        held_branches=[{"branch": "renovate/app", "marker": "unpend", "apps": ["app"]}],
        raises=(Raised(title=MOVED, presses=(OPEN_PR,)),),
    ),
    update(compose=["compose/app/compose.yaml"], raises=(Raised(title=MOVED, presses=(OPEN_PR,)),)),
    update(compose=["compose/app/docker-compose.yml"], raises=(MOVED,)),
)


EXAMPLES = (
    Examples(
        "container-image-advisory",
        fires=(
            # As urgent as what reaches it: where HQ does not know what reaches it, as if anyone may.
            # A watched one leads with the move its plan recommends, on its page, and says how to make it.
            box(
                advisories=[HIGH],
                declared={"app": {}},
                raises=(
                    Raised(
                        title=ADVISED,
                        severity=Tone.SERIOUS,
                        primary=("Upgrade to v1.3.0 (fixes 1)", PLANNED),
                        steps=(SET_APP,),
                    ),
                ),
            ),
            box(advisories=[HIGH], reach=OPEN, raises=(Raised(title=ADVISED, severity=Tone.SERIOUS),)),
            box(advisories=[HIGH], reach=PRIVATE, raises=(ADVISED,)),
            box(advisories=[HIGH], reach=UNROUTED, raises=(Raised(title=ADVISED, severity=Tone.NEUTRAL),)),
            # Many advisories are one thing to do.
            box(
                advisories=[HIGH, {**HIGH, "id": "GHSA-high-2"}, {**HIGH, "id": "GHSA-high-3"}],
                reach=OPEN,
                raises=(Raised(title="ghcr.io/example/app:v1.2.0 has 3 known vulnerabilities", severity=Tone.SERIOUS),),
            ),
            held("no release fixes it yet", advisories=[HIGH], app_tags=("v1.2.0",)),
            held("v1.3.0 is not known to fix it", advisories=[UNFIXED]),
            # Whatever it fixes, a release that brings its own is no move to make.
            held(
                "v1.3.0 has a known vulnerability of its own",
                ("Upgrade plan", PLANNED),
                advisories=[HIGH, BROUGHT],
                declared={"app": {}},
            ),
        ),
        # A version no release fixes is said only where the internet may reach it.
        quiet=(box(), box(advisories=[UNFIXED], reach=PRIVATE)),
    ),
    Examples(
        "container-image-update",
        fires=(
            # No repository holds them: a person moves each by hand, and a major move says so where the card leads.
            box(
                raises=(
                    Raised(title="app has an update, v1.2.0 to v1.3.0", steps=(SET_APP,)),
                    Raised(title=KUMA, primary=("Upgrade to 2 (major)", LISTED)),
                )
            ),
            # A newer release that brings a vulnerability is no update to make.
            box(advisories=[BROUGHT], raises=(KUMA,)),
            *STATES,
        ),
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
