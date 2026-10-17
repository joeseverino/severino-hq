"""The containers on the host: what Portainer reads of them, and what the
public registries say about the images they run."""

from datetime import timedelta

from hq.domains.control_plane.observations import portainer
from hq.domains.control_plane.observations.public_registry import (
    DIGEST_KIND,
    IMAGE_KIND,
    UPSTREAM_KIND,
    VULNERABILITY_KIND,
)
from hq.platform.application.images import ImageRef
from hq.platform.application.inventory_testing import store
from hq.platform.application.services import CONTAINER_KIND

from .names import CONTAINERS, LAN, MACHINE, PORTAINER, SOURCES, container_id, digest, hexed, iso, stamp

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
            "containers_running": len(CONTAINERS),
            "containers_total": len(CONTAINERS),
            "snapshot_at": iso(timedelta(minutes=4)),
        },
    )
    _registries()


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
        "image": pinned(image),
        "state": "running",
        "status": "Up 12 days (healthy)",
        "ports": [port],
        "network_mode": "bridge",
        "portainer_managed": False,
    }


def _ref(image):
    return ImageRef.parse(image)


def pinned(image):
    """The image locked to the build that runs, as a well-kept compose file names it."""

    return image if _ref(image).digest else f"{image}@{running_digest(image)}"


def running_digest(image):
    """The digest a container's image was pulled at: its own pin, or one derived from its tag."""

    ref = _ref(image)
    return ref.digest or digest(ref.name, ref.tag)


def _pulled(name, image):
    ref = _ref(image)
    return _on_machine(
        id=digest("image-id", name),
        tags=[f"{ref.name}:{ref.tag}"],
        digests=[f"{ref.name}@{running_digest(image)}"],
        created_at=iso(UP_FOR + timedelta(days=2)),
        size=180_000_000 + len(name) * 9_000_000,
        containers=[{"container": name, "reference": pinned(image)}],
    )


def _runtime(name, port):
    mounts = [{"type": "volume", "source": f"{name}_data", "destination": "/data", "read_only": False}]
    if name == "portainer":
        mounts.append(
            {"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock", "read_only": True}
        )
    return _on_machine(
        container=name,
        stack=name,
        service=name,
        image_id=digest("image-id", name),
        **KEPT,
        mounts=mounts,
        port_bindings=[{"container_port": f"{port}/tcp", "host_ip": LAN, "host_port": str(port)}],
        exposed_ports=[port],
        started_at=iso(UP_FOR),
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


def _registries():
    """Each image as its registry publishes it, its source's releases, what its
    publisher attached to the digest that runs, and those packages checked."""

    published, upstreams, attested, checked = [], [], [], []
    for name, _port, image in CONTAINERS:
        ref = _ref(image)
        source = SOURCES[name]
        pinned = f"{ref.name}@{running_digest(image)}"
        published.append(
            {
                "image": ref.name,
                "tags": [ref.tag],
                "digests": {ref.tag: running_digest(image)},
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
                    {
                        "tag": ref.tag,
                        "url": f"{source}/releases/tag/{ref.tag}",
                        "published_at": stamp(timedelta(days=20)),
                    }
                ],
                "advisories": [],
                "read_at": stamp(timedelta(hours=3)),
            }
        )
        attested.append(
            {
                "digest": pinned,
                "image": ref.name,
                "platform_digest": digest("platform", pinned),
                "packages": list(PACKAGES),
                "sbom": "spdx",
                "provenance": {
                    "format": "https://slsa.dev/provenance/v1",
                    "source": source,
                    "revision": hexed("revision", pinned, length=40),
                    "builder": "https://github.com/actions/runner",
                    "finished_at": stamp(timedelta(days=21)),
                    "materials": [f"pkg:docker/alpine@3.20?digest={digest('alpine', '3.20')}"],
                },
                "read_at": stamp(timedelta(hours=3)),
            }
        )
        checked.append(
            {"digest": pinned, "checked": len(PACKAGES), "findings": [], "read_at": stamp(timedelta(hours=3))}
        )
    store(IMAGE_KIND, *published)
    store(UPSTREAM_KIND, *upstreams)
    store(DIGEST_KIND, *attested)
    store(VULNERABILITY_KIND, *checked)
