"""The containers on the host: what Portainer reads of them, and what the
public registries say about the images they run."""

from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import portainer
from hq.domains.control_plane.observations.public_registry import (
    DIGEST_KIND,
    IMAGE_KIND,
    UPSTREAM_KIND,
    VULNERABILITY_KIND,
)
from hq.platform.application.docker_estate import release_pins
from hq.platform.application.images import ImageRef
from hq.platform.application.services import CONTAINER_KIND

from .names import (
    BUILT,
    CONTAINERS,
    LAN,
    MACHINE,
    NEWER,
    PORTAINER,
    SOURCES,
    container_id,
    digest,
    hexed,
    iso,
    stamp,
)
from .sweep import store

UP_FOR = timedelta(days=12)
# How a well-run container is run: a user, no-new-privileges, a limit, a check.
KEPT = {
    "user": "1000:1000",
    "security_opt": ["no-new-privileges:true"],
    "cap_drop": ["ALL"],
    "memory_limit": 1_073_741_824,
    "pids_limit": 512,
    "healthcheck": True,
    "health": "healthy",
    "restart_policy": "unless-stopped",
    "restart_count": 0,
    "network_mode": "bridge",
}
# The packages each image's SBOM lists: a few, named the way OSV reads them.
PACKAGES = ("pkg:apk/alpine/musl@1.2.5-r1", "pkg:apk/alpine/openssl@3.3.2-r1", "pkg:apk/alpine/zlib@1.3.1-r1")
# The one container its compose file starts by tag alone, so the machine holds
# the digest it could be pinned to and HQ names that pin.
BY_TAG = "nginx-proxy-manager"
# The DNS server tailnet devices use, on the host's network its job needs and
# as root, as its image starts it: binding port 53 there takes NET_BIND_SERVICE,
# which its dropped capabilities leave out.
HOST_NETWORK = "adguard"
DNS_PORT = 53


def containers():
    """Every container, running and healthy, with all Portainer reads of it."""

    store(CONTAINER_KIND, *(_running(name, port, image) for name, port, image in CONTAINERS))
    store(portainer.IMAGE_KIND, *(_pulled(name, image) for name, _port, image in CONTAINERS))
    store(portainer.RUNTIME_KIND, *(_runtime(name, port) for name, port, _image in CONTAINERS))
    store(portainer.VOLUME_KIND, *_volumes())
    store(portainer.STACK_KIND, *(_stack(name) for name, _port, _image in CONTAINERS))
    store(portainer.NETWORK_KIND, _network())
    store(
        portainer.ENVIRONMENT_KIND,
        {
            "connection_ref": PORTAINER,
            "id": 1,
            "name": MACHINE,
            "host": MACHINE,
            "address": LAN,
            "local": True,
            "type": "docker",
            "status": "up",
            "agent_version": "2.24.0",
            "docker_version": "27.3.1",
            "snapshot_at": iso(timedelta(minutes=4)),
        },
    )
    _registries()


# How long before the empty read the containers were last listed.
LISTED_BEFORE = timedelta(hours=3)


def read_empty():
    """The host's environment answering with no containers, as it does once
    Docker restarts under Portainer: every read through it lands and lists
    nothing, while each container HQ watches was last listed a few hours
    before."""

    for kind in (
        CONTAINER_KIND,
        portainer.IMAGE_KIND,
        portainer.RUNTIME_KIND,
        portainer.VOLUME_KIND,
        portainer.STACK_KIND,
        portainer.NETWORK_KIND,
    ):
        store(kind)
    ManagedResource.objects.filter(kind=CONTAINER_KIND).update(last_observed_at=timezone.now() - LISTED_BEFORE)


def _on_machine(**fields):
    return {"connection_ref": PORTAINER, "environment_id": 1, "host": MACHINE, "host_address": LAN, **fields}


def _running(name, port, image):
    return {
        "id": container_id(name),
        "name": name,
        "stack": name,
        "host": MACHINE,
        "host_address": LAN,
        "connection_ref": PORTAINER,
        "image": started_by(name, image),
        "state": "running",
        "status": "Up 12 days (healthy)",
        # Docker reports no published port on the host's network.
        "ports": [] if name == HOST_NETWORK else [port],
        "network_mode": "host" if name == HOST_NETWORK else "bridge",
        "portainer_managed": False,
    }


def _ref(image):
    return ImageRef.parse(image)


def pinned(image):
    """The image locked to the build that runs, as a well-kept compose file names it."""

    return image if _ref(image).digest else f"{image}@{running_digest(image)}"


def started_by(name, image):
    """The reference a container was started by: pinned, but for the one
    started by tag alone and the one its host builds, which no digest names."""

    return image if name in (BY_TAG, BUILT) else pinned(image)


def running_digest(image):
    """The digest a container's image was pulled at: its own pin, or one derived from its tag."""

    ref = _ref(image)
    return ref.digest or digest(ref.name, ref.tag)


def _pulled(name, image):
    ref = _ref(image)
    # Docker keeps a build's tag as written, and no registry digest for it.
    built = name == BUILT
    return _on_machine(
        id=digest("image-id", name),
        tags=[image if built else f"{ref.name}:{ref.tag}"],
        digests=[] if built else [f"{ref.name}@{running_digest(image)}"],
        created_at=iso(UP_FOR + timedelta(days=2)),
        size=180_000_000 + len(name) * 9_000_000,
        containers=[{"container": name, "reference": started_by(name, image), "service": name}],
    )


def _runtime(name, port):
    mounts = [{"type": "volume", "source": f"{name}_data", "destination": "/data", "read_only": False}]
    if name == "portainer":
        mounts.append(
            {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}
        )
    on_host = name == HOST_NETWORK
    return _on_machine(
        container=name,
        stack=name,
        service=name,
        image_id=digest("image-id", name),
        **KEPT | ({"network_mode": "host", "user": ""} if on_host else {}),
        mounts=mounts,
        port_bindings=[] if on_host else [{"container_port": f"{port}/tcp", "host_ip": LAN, "host_port": str(port)}],
        exposed_ports=[DNS_PORT, port] if on_host else [port],
        started_at=iso(UP_FOR),
        # What it used at the last sweep: a few hundred megabytes, a little CPU.
        memory_usage=(48 + 16 * len(name)) * 1_048_576,
        memory_usage_basis="anon",
        cpu_time=int(UP_FOR.total_seconds() * 1e9 * len(name) / 100),
        usage_read_at=iso(timedelta(minutes=4)),
        cpu_percent=round(0.4 + len(name) / 10, 1),
    )


def _volumes():
    return [
        _on_machine(
            type="volume",
            name=f"{name}_data",
            driver="local",
            stack=name,
            created_at=iso(timedelta(days=200)),
            used_by=[{"container": name, "destination": "/data", "read_only": False}],
        )
        for name, _port, _image in CONTAINERS
    ]


def _stack(name):
    return _on_machine(
        name=name,
        source="compose",
        working_dir=f"/opt/apps/{name}",
        config_files=[f"/opt/apps/{name}/compose.yaml"],
        containers=[name],
    )


def _network():
    return _on_machine(
        id=hexed("network", "proxy"),
        name="proxy",
        driver="bridge",
        scope="local",
        subnets=["172.18.0.0/16"],
        containers=[name for name, _port, _image in CONTAINERS],
    )


def _advisory(name, vulnerable, patched=""):
    return {
        "id": f"GHSA-example-{name}",
        "severity": "high",
        "summary": f"Example advisory against {name}",
        "url": f"https://github.com/advisories/GHSA-example-{name}",
        "vulnerabilities": [[vulnerable, patched]],
    }


# Each upgrade a card can offer: a release that fixes what runs, one still
# affected, one that brings an advisory of its own, and a major move.
ADVISED_NEWER = {
    **NEWER,
    "immich": "v1.120.1",
    "jellyfin": "10.10.1",
    "forgejo": "9.0.2",
    "adguard": "v1.0.0",
}
ADVISED = {
    "immich": [_advisory("immich", "< 1.120.1", "1.120.1")],
    "jellyfin": [_advisory("jellyfin", "< 10.11.0")],
    "forgejo": [_advisory("forgejo-running", "< 9.0.2", "9.0.2"), _advisory("forgejo-target", ">= 9.0.2")],
}


# A newer release of every app the apps repository holds an update of in a
# state of its own (``work.update_states``), and of one no repository holds.
UPDATES = {
    **NEWER,
    "immich": "v1.120.1",
    "adguard": "v1.0.0",
    "nginx-proxy-manager": "2.12.2",
    "portainer": "2.24.1",
}


def updates_out():
    """The registries with a newer release of each app in ``UPDATES``."""

    _registries(UPDATES)


def images_left():
    """Images nothing runs: an earlier pull of a container's image and an
    untagged one, which HQ offers to remove, and each image the release pins,
    which it keeps."""

    _name, _port, image = CONTAINERS[0]
    earlier = _ref(image).name
    pins = [ref for pin in release_pins() if (ref := ImageRef.parse(pin)) is not None]
    store(
        portainer.IMAGE_KIND,
        *(_pulled(name, image) for name, _port, image in CONTAINERS),
        _on_machine(id=digest("image-id", "earlier"), tags=[f"{earlier}:0.9"], size=240_000_000, containers=[]),
        _on_machine(id=digest("image-id", "untagged"), size=60_000_000, containers=[]),
        *(
            _on_machine(id=digest("image-id", ref.name), digests=[f"{ref.name}@{ref.digest}"], size=900_000_000)
            for ref in pins
        ),
    )


def advised():
    """The registries and advisories of ``ADVISED``: each upgrade card a
    vulnerable or older image can draw."""

    _registries(ADVISED_NEWER, ADVISED)


def _registries(newer=NEWER, advisories=None):
    """Each image as its registry publishes it, its source's releases, what its
    publisher attached to each digest it publishes, and those packages checked.
    What runs was released three weeks ago; a newer release, where there is
    one, four days ago."""

    published, upstreams, attested, checked = [], [], [], []
    for name, _port, image in CONTAINERS:
        if name == BUILT:
            continue
        ref = _ref(image)
        source = SOURCES[name]
        releases = {ref.tag: (running_digest(image), timedelta(days=20))}
        if name in newer:
            releases[newer[name]] = (digest(ref.name, newer[name]), timedelta(days=4))
        published.append(
            {
                "image": ref.name,
                "tags": list(releases),
                "digests": {tag: pin for tag, (pin, _age) in releases.items()},
                "source": source,
                "version": ref.tag.removeprefix("v"),
                "read_at": stamp(timedelta(hours=3)),
            }
        )
        upstreams.append(
            {
                "repository": source.removeprefix("https://github.com/"),
                "url": source,
                "releases": [
                    {"tag": tag, "url": f"{source}/releases/tag/{tag}", "published_at": stamp(age)}
                    for tag, (_pin, age) in reversed(releases.items())
                ],
                "advisories": list((advisories or {}).get(name, ())),
                "read_at": stamp(timedelta(hours=3)),
            }
        )
        for pin, age in releases.values():
            attested.append(_attested(f"{ref.name}@{pin}", ref.name, source, age))
            checked.append(
                {
                    "digest": f"{ref.name}@{pin}",
                    "checked": len(PACKAGES),
                    "findings": [],
                    "read_at": stamp(timedelta(hours=3)),
                }
            )
    store(IMAGE_KIND, *published)
    store(UPSTREAM_KIND, *upstreams)
    store(DIGEST_KIND, *attested)
    store(VULNERABILITY_KIND, *checked)


def _attested(pinned, image, source, released):
    return {
        "digest": pinned,
        "image": image,
        "platform_digest": digest("platform", pinned),
        "packages": list(PACKAGES),
        "sbom": "spdx",
        "provenance": {
            "format": "https://slsa.dev/provenance/v1",
            "source": source,
            "revision": hexed("revision", pinned, length=40),
            "builder": "https://github.com/actions/runner",
            "finished_at": stamp(released + timedelta(days=1)),
            # A package URL, whose qualifier keeps the digest's colon as written.
            # ast-grep-ignore: query-added-by-with-query
            "materials": [f"pkg:docker/alpine@3.20?digest={digest('alpine', '3.20')}"],
        },
        "read_at": stamp(timedelta(hours=3)),
    }
