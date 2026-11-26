"""The standard a running container is held to: how much of its machine it can reach.

Read from Docker's inspect of each container (``portainer.runtime``), never
from inside it; what the image itself is held to is ``application.supply_chain``.
Nothing here is a verdict on intent: a socket proxy exists to hold the Docker
socket, and the page says so as plainly as for anything else.
"""

from collections.abc import Mapping
from functools import partial
from types import SimpleNamespace as Row
from typing import Any

from django.utils.text import get_text_list

lazy from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from .standards import Check, Examples, Measurement, Reading, measure
lazy from .container_findings import help_for
lazy from .container_hardening import hardening_of
lazy from .containers import Container, ImageStanding, by_design, socket_holders
lazy from .exposure import listening_ports
lazy from .images import ImageRef
lazy from .ui import ended

# Host paths whose write access is the machine's, not the container's: the
# path and everything under it.
SYSTEM_PATHS = ("/boot", "/dev", "/etc", "/proc", "/root", "/sys", "/usr", "/var/lib/docker")
# The machine's own only as themselves: a directory under /run is the runtime
# state of whatever made it, an application's own.
SYSTEM_ROOTS = ("/", "/run", "/var/run")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
# Capabilities that are the machine's in all but name.
POWERFUL_CAPABILITIES = frozenset(
    {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "DAC_READ_SEARCH", "NET_ADMIN", "BPF"}
)
ROOT_USERS = frozenset({"", "0", "root", "0:0", "root:root"})
_ANY_ADDRESS = frozenset({"", "0.0.0.0", "::"})  # noqa: S104 - addresses a binding is held against, never bound


def _runtime(container: Any) -> Mapping[str, Any] | None:
    return container.runtime


RUNTIME_UNREAD = "Its runtime has not been read"
MOUNTS_UNREAD = "Its mounts have not been read"


def _on(test):
    """A check of the runtime reading, unavailable when it was not read."""

    def check(container: Any) -> Reading:
        runtime = _runtime(container)
        return Reading.unavailable(RUNTIME_UNREAD) if runtime is None else Reading.judged(test(runtime))

    return check


# Emptied by every boot: state for this run of something, never kept.
RUNTIME_ROOTS = ("/run", "/var/run", "/tmp", "/dev/shm")  # noqa: S108 - paths a mount is classified by, never opened


def runtime_path(source: str) -> bool:
    path = source.rstrip("/") or "/"
    return any(path == root or path.startswith(f"{root}/") for root in RUNTIME_ROOTS)


def system_path(source: str) -> bool:
    path = source.rstrip("/") or "/"
    return path in SYSTEM_ROOTS or any(path == root or path.startswith(f"{root}/") for root in SYSTEM_PATHS)


def _socket_held(container: Any) -> Reading:
    """No Docker socket, or one its declaration says it exists to hold.

    A socket proxy or a management agent cannot do its job without the
    socket, so flagging it would raise an action item nobody can clear. The
    operator says so once, on the container, and anything else that mounts
    the socket is still flagged.
    """

    mounts = container.mounts
    if mounts is None:
        return Reading.unavailable(MOUNTS_UNREAD)
    return Reading.judged(
        _no_socket([mount for mount in mounts if mount.get("type") == "bind"]) or holds_socket(container)
    )


def _no_socket(binds) -> bool:
    return not any(mount.get("source") in DOCKER_SOCKETS for mount in binds)


def writable_system_binds(binds, *, docker_held: bool = False) -> tuple[Mapping[str, Any], ...]:
    """The bind mounts that let it write a system path."""

    return tuple(
        mount
        for mount in binds
        if (source := str(mount.get("source", ""))) not in DOCKER_SOCKETS
        and system_path(source)
        and not mount.get("read_only")
        # Docker's own data is no more than its socket already reaches, for a
        # container declared to hold the socket (a management agent reads
        # volumes there). Any other system path still counts.
        and not (docker_held and (source == "/var/lib/docker" or source.startswith("/var/lib/docker/")))
    )


def holds_socket(container: Any) -> bool:
    """Whether its declaration says holding the Docker socket is its job."""

    return (container.machine.name, container.running.name) in socket_holders()


def _system_path_kept(container: Any) -> Reading:
    mounts = container.mounts
    if mounts is None:
        return Reading.unavailable(MOUNTS_UNREAD)
    binds = [mount for mount in mounts if mount.get("type") == "bind"]
    return Reading.judged(not writable_system_binds(binds, docker_held=holds_socket(container)))


def _confined(runtime: Mapping[str, Any]) -> bool:
    return not any(option.endswith(("=unconfined", ":unconfined")) for option in runtime.get("security_opt") or ())


def _no_powerful_capability(runtime: Mapping[str, Any]) -> bool:
    return not (set(runtime.get("cap_add") or ()) & POWERFUL_CAPABILITIES)


def _health_checked(container: Any) -> Reading:
    """Its own health check, or a name it serves that HQ requests: either
    says whether it works. An image with no shell cannot run the first."""

    runtime = _runtime(container)
    if runtime is None:
        return Reading.unavailable(RUNTIME_UNREAD)
    return Reading.judged(bool(runtime.get("healthcheck")) or bool(container.running.check) or bool(container.serves))


def _bound_to_an_address(runtime: Mapping[str, Any]) -> bool:
    return not any(binding.get("host_ip", "") in _ANY_ADDRESS for binding in runtime.get("port_bindings") or ())


def _bind(source: str, *, read_only: bool = False) -> dict[str, Any]:
    return {"type": "bind", "source": source, "destination": source, "read_only": read_only}


# A serious check is reach over the machine; the rest is hardening that limits
# what a compromised container could do, or what an upgrade could be verified
# by.
STANDARD: tuple[Check, ...] = (
    Check(
        "not-privileged",
        "Not privileged",
        _on(lambda runtime: not runtime.get("privileged")),
        "A privileged container is root on its machine: every device and every kernel capability.",
        "Remove privileged mode and add only the capability it needs.",
        serious=True,
        unmet="Runs privileged",
        breaks=({"privileged": True},),
    ),
    Check(
        "no-docker-socket",
        "No Docker socket",
        _socket_held,
        "The Docker socket can start a root container with any mount, even mounted read-only.",
        "Put a socket proxy that allows only the calls it needs between it and the socket.",
        serious=True,
        unmet="Holds the Docker socket",
        breaks=({"mounts": [_bind(DOCKER_SOCKETS[0], read_only=True)]},),
    ),
    Check(
        "own-process-namespace",
        "Does not share the host's process list",
        _on(lambda runtime: runtime.get("pid_mode") != "host"),
        "Sharing the host's process namespace lets it see and signal every process on the machine.",
        "Remove the host PID mode.",
        serious=True,
        unmet="Shares the host's process list",
        breaks=({"pid_mode": "host"},),
    ),
    Check(
        "confined",
        "Confined by seccomp and AppArmor",
        _on(_confined),
        "An unconfined container can make any system call the kernel offers.",
        "Remove the unconfined security option.",
        serious=True,
        unmet="Not confined by seccomp and AppArmor",
        breaks=({"security_opt": ["seccomp=unconfined", "no-new-privileges:true"]},),
    ),
    Check(
        "no-system-path-writable",
        "No system path writable",
        _system_path_kept,
        "A writable mount of a system path is a way to change the machine from inside the container.",
        "Mount it read-only, or mount only the one file it needs.",
        serious=True,
        unmet="Can write a system path",
        breaks=(
            {"mounts": [_bind("/etc")]},
            {"mounts": [_bind("/var/lib/docker/volumes")]},
            {"mounts": [_bind("/run")]},
        ),
        keeps=(
            {"mounts": [_bind("/etc/localtime", read_only=True), _bind("/opt/apps/web/data")]},
            {"mounts": [_bind("/run/web")]},
        ),
    ),
    Check(
        "no-powerful-capability",
        "No extra kernel capabilities",
        _on(_no_powerful_capability),
        "Some capabilities (SYS_ADMIN, NET_ADMIN, SYS_PTRACE and the like) give it nearly everything root on the machine has.",
        "Drop the capability, or narrow it to the one operation it needs.",
        serious=True,
        unmet="Has an extra kernel capability",
        breaks=({"cap_add": ["SYS_ADMIN"]},),
    ),
    Check(
        "not-root",
        "Runs as a user other than root",
        _on(lambda runtime: str(runtime.get("user", "")) not in ROOT_USERS),
        "A process running as root inside a container becomes root on the machine if it escapes.",
        "Set a non-root user in the compose file, or use an image that runs as one.",
        unmet="Runs as root",
        breaks=({"user": "root"}, {"user": ""}),
    ),
    Check(
        "no-new-privileges",
        "Cannot gain privileges",
        _on(
            lambda runtime: any(
                option.startswith("no-new-privileges") and not option.endswith("false")
                for option in runtime.get("security_opt") or ()
            )
        ),
        "Without it, a setuid program inside the container can gain more privileges.",
        "Add no-new-privileges:true to its security options.",
        unmet="Can gain privileges",
        breaks=({"security_opt": []}, {"security_opt": ["no-new-privileges:false"]}),
    ),
    Check(
        "no-devices",
        "No host devices",
        _on(lambda runtime: not runtime.get("devices")),
        "A passed-through device is direct access to that hardware.",
        "Remove the device unless the service cannot work without it.",
        unmet="Has a host device",
        breaks=({"devices": ["/dev/ttyUSB0"]},),
    ),
    Check(
        "own-network",
        "Not on the host network",
        _on(lambda runtime: runtime.get("network_mode") != "host"),
        "On the host network, every port it opens is the machine's, on every interface.",
        "Give it a bridge network and publish only the ports it serves.",
        unmet="On the host network",
        breaks=({"network_mode": "host"},),
    ),
    Check(
        "ports-bound",
        "Ports bound to an address",
        _on(_bound_to_an_address),
        "A port published on every interface is reachable from every network the machine is on.",
        "Publish it on the address it is meant to be reached at, 127.0.0.1 behind a proxy.",
        unmet="Publishes a port on every interface",
        breaks=({"port_bindings": [{"container_port": "80/tcp", "host_ip": "", "host_port": "8080"}]},),
    ),
    Check(
        "memory-limited",
        "Memory limited",
        _on(lambda runtime: bool(runtime.get("memory_limit"))),
        "Without a limit, one runaway container can take the machine's memory from every other.",
        "Set a memory limit in the compose file.",
        unmet="No memory limit",
        breaks=({"memory_limit": 0},),
    ),
    Check(
        "health-checked",
        "Has a health check",
        _health_checked,
        "Without a check, HQ can only see that it is running, and cannot tell whether an upgrade worked.",
        "Add a health check to the compose file, or serve it under a name HQ checks.",
        unmet="No health check",
        breaks=({"healthcheck": False},),
        # An image with no shell cannot run a check; a name HQ requests still says.
        keeps=({"healthcheck": False, "serves": ["admin.example.com"]},),
    ),
)


def posture_of(container: Any) -> Measurement:
    declared = by_design().get((container.machine.name, container.running.name)) or {}
    return measure(
        container,
        STANDARD,
        {**_required(container, declared), **declared},
        helps=partial(help_for, container),
        reads=CONTAINER_KIND,
    )


def _required(container: Any, declared: Mapping[str, str]) -> dict[str, str]:
    """The checks its job requires it to fail, each with why.

    The host network, for a port a role of its machine is reached on, since
    there it answers at the machine's own address and sees each client's. And
    root, for a port under 1024 it binds there under no-new-privileges, held
    with no capability but the one that binds it (``ROOT_POSTURE``).
    """

    required = {}
    answered = set(listening_ports(container)) if container.running.network_mode == "host" else set()
    for role in container.machine.roles:
        if owned := [str(port) for port in role.ports if port in answered]:
            required["own-network"] = ended(f"{role.detail.removesuffix('.')} on port {get_text_list(owned, 'and')}")
            break
    runtime = container.runtime or {}
    if (ports := root_ports(container, waived="no-new-privileges" in declared)) and holds_root_posture(runtime):
        required["not-root"] = ended(
            f"It binds {get_text_list(ports, 'and')} on the host's network under no-new-privileges, which only "
            "root does, and holds no capability but NET_BIND_SERVICE"
        )
    return required


# The strongest posture a service that must run as root still works under:
# every capability dropped but the one that binds a port under 1024.
ROOT_POSTURE = 'cap_drop:\n  - "ALL"\ncap_add:\n  - "NET_BIND_SERVICE"'


def privileged_ports(container: Any) -> list[str]:
    """The ports under 1024 it binds on the host's own network, which take
    NET_BIND_SERVICE. In a network of its own Docker lets any user bind them
    (moby's daemon/oci_linux.go sets net.ipv4.ip_unprivileged_port_start to 0
    for a private network only)."""

    if container.running.network_mode != "host":
        return []
    return [str(port) for port in sorted(listening_ports(container)) if port < 1024]


def root_ports(container: Any, *, waived: bool) -> list[str]:
    """The privileged ports that make root its only working user: unless
    no-new-privileges is ``waived`` by design, the standard holds it to that,
    which ignores a file capability on its program, and Docker gives a user
    other than root no ambient capability (moby#8460, the fix reverted in
    moby#26979; moby#45491 for no-new-privileges)."""

    return [] if waived else privileged_ports(container)


def holds_root_posture(runtime: Mapping[str, Any]) -> bool:
    return "ALL" in (runtime.get("cap_drop") or ()) and set(runtime.get("cap_add") or ()) <= {"NET_BIND_SERVICE"}


CHECK_IDS = frozenset(check.id for check in STANDARD)


def example(runtime: Mapping[str, Any] | None = None, **standing: Any) -> Any:
    """A container on example-box run as ``runtime`` says (``serves`` the
    names HQ requests of it), nothing read of how when it is empty, and its
    image as ``standing`` says."""

    record = dict(runtime or {})
    serves = tuple(record.pop("serves", ()))
    return Container(
        running=Row(name="web", check=None, watcher="", network_mode=record.get("network_mode", ""), ports=()),
        machine=Row(name="example-box", roles=()),
        standing=ImageStanding(
            **{"image": ImageRef("ghcr.io", "example/web", "1.0"), "tag": "1.0", "pinned": True, **standing}
        ),
        serves=serves,
        runtime=record or None,
    )


def _fixed(container: Any, check: Check) -> bool:
    hardening = hardening_of(container)
    return hardening.covers(check.id) or any(gap.check == check.id and gap.reason for gap in hardening.unwritten)


# How a well-run container reads: a user, no-new-privileges, a limit, a check.
EXAMPLES = Examples(
    checks=STANDARD,
    subject=example,
    kept={
        "user": "1000",
        "security_opt": ["no-new-privileges:true"],
        "memory_limit": 268435456,
        "healthcheck": True,
        "network_mode": "bridge",
        "port_bindings": [{"container_port": "80/tcp", "host_ip": "127.0.0.1", "host_port": "8080"}],
    },
    fixed=_fixed,
)
