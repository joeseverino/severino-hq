"""The standard a running container is held to: how much of its machine it can reach.

Read from Docker's inspect of each container (``portainer.runtime``), never
from inside it; what the image itself is held to is
``application.supply_chain``. Serious is reach over the machine
(privilege, the Docker socket, the host's process namespace, confinement turned
off, a writable system path); the rest is hardening that limits what a
compromised container could do, or what an upgrade could be verified by.

Nothing here is a verdict on intent: a socket proxy exists to hold the Docker
socket, and the page says so as plainly as for anything else.
"""

from __future__ import annotations

from typing import Any, Mapping

from .standards import Check, Posture, measure

# Host paths whose write access is the machine's, not the container's: the
# path and everything under it.
SYSTEM_PATHS = ("/boot", "/dev", "/etc", "/proc", "/root", "/sys", "/usr", "/var/lib/docker")
# The machine's own only as themselves: a directory under /run is the runtime
# state of whatever made it, an application's own.
SYSTEM_ROOTS = ("/", "/run", "/var/run")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
# Capabilities that are the machine's in all but name.
POWERFUL_CAPABILITIES = frozenset({"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "DAC_READ_SEARCH", "NET_ADMIN", "BPF"})
ROOT_USERS = frozenset({"", "0", "root", "0:0", "root:root"})
_ANY_ADDRESS = frozenset({"", "0.0.0.0", "::"})


def _runtime(container: Any) -> Mapping[str, Any] | None:
    return container.runtime


def _on(test):
    """A check of the runtime reading, unavailable when it was not read."""

    def check(container: Any) -> bool | None:
        runtime = _runtime(container)
        return None if runtime is None else test(runtime)

    return check


def _mounted(test):
    """A check of its mounts, from whichever reading has them."""

    def check(container: Any) -> bool | None:
        mounts = container.mounts
        return None if mounts is None else test([mount for mount in mounts if mount.get("type") == "bind"])

    return check


# Emptied by every boot: state for this run of something, never kept.
RUNTIME_ROOTS = ("/run", "/var/run", "/tmp", "/dev/shm")


def runtime_path(source: str) -> bool:
    path = source.rstrip("/") or "/"
    return any(path == root or path.startswith(f"{root}/") for root in RUNTIME_ROOTS)


def system_path(source: str) -> bool:
    path = source.rstrip("/") or "/"
    return path in SYSTEM_ROOTS or any(path == root or path.startswith(f"{root}/") for root in SYSTEM_PATHS)


def _socket_held(container: Any) -> bool | None:
    """No Docker socket, or one its declaration says it exists to hold.

    A socket proxy or a management agent cannot do its job without the
    socket, so flagging it would raise an action item nobody can clear. The
    operator says so once, on the container, and anything else that mounts
    the socket is still flagged.
    """

    mounts = container.mounts
    if mounts is None:
        return None
    if _no_socket([mount for mount in mounts if mount.get("type") == "bind"]):
        return True
    return holds_socket(container)


def _no_socket(binds) -> bool:
    return not any(mount.get("source") in DOCKER_SOCKETS for mount in binds)


def writable_system_binds(binds, docker_held: bool = False) -> tuple[Mapping[str, Any], ...]:
    """The bind mounts that let it write a system path."""

    return tuple(
        mount
        for mount in binds
        if (source := str(mount.get("source", ""))) not in DOCKER_SOCKETS
        and system_path(source) and not mount.get("read_only")
        # Docker's own data is no more than its socket already reaches, for a
        # container declared to hold the socket (a management agent reads
        # volumes there). Any other system path still counts.
        and not (docker_held and (source == "/var/lib/docker" or source.startswith("/var/lib/docker/")))
    )


def holds_socket(container: Any) -> bool:
    """Whether its declaration says holding the Docker socket is its job."""

    from .containers import socket_holders

    return (container.machine.name, container.running.name) in socket_holders()


def _system_path_kept(container: Any) -> bool | None:
    mounts = container.mounts
    if mounts is None:
        return None
    binds = [mount for mount in mounts if mount.get("type") == "bind"]
    return not writable_system_binds(binds, holds_socket(container))


def _confined(runtime: Mapping[str, Any]) -> bool:
    return not any(option.endswith("=unconfined") or option.endswith(":unconfined") for option in runtime.get("security_opt") or ())


def _no_powerful_capability(runtime: Mapping[str, Any]) -> bool:
    return not (set(runtime.get("cap_add") or ()) & POWERFUL_CAPABILITIES)


def _bound_to_an_address(runtime: Mapping[str, Any]) -> bool:
    return not any(binding.get("host_ip", "") in _ANY_ADDRESS for binding in runtime.get("port_bindings") or ())


STANDARD: tuple[Check, ...] = (
    Check("not-privileged", "Not privileged", _on(lambda runtime: not runtime.get("privileged")),
          "A privileged container is root on its machine: every device and every kernel capability.",
          "Remove privileged mode and add only the capability it needs.", serious=True),
    Check("no-docker-socket", "No Docker socket", _socket_held,
          "The Docker socket starts any container, mounting anything, as root: read-only or not.",
          "Put a socket proxy that allows only the calls it needs between it and the socket.", serious=True),
    Check("own-process-namespace", "Its own processes only", _on(lambda runtime: runtime.get("pid_mode") != "host"),
          "Sharing the host's process namespace lets it see and signal every process on the machine.",
          "Remove the host PID mode.", serious=True),
    Check("confined", "Confined by seccomp and AppArmor", _on(_confined),
          "An unconfined container can make any system call the kernel offers.",
          "Remove the unconfined security option.", serious=True),
    Check("no-system-path-writable", "No system path writable", _system_path_kept,
          "A writable mount of a system path is a way to change the machine from inside the container.",
          "Mount it read-only, or mount only the one file it needs.", serious=True),
    Check("no-powerful-capability", "No machine-level capability", _on(_no_powerful_capability),
          "Some capabilities (SYS_ADMIN, NET_ADMIN, SYS_PTRACE and the like) are root in all but name.",
          "Drop the capability, or narrow it to the one operation it needs.", serious=True),
    Check("not-root", "Runs as a user other than root", _on(lambda runtime: str(runtime.get("user", "")) not in ROOT_USERS),
          "Root inside a container is one kernel bug away from root on the machine.",
          "Set a non-root user in the compose file, or use an image that runs as one."),
    Check("no-new-privileges", "Cannot gain privileges", _on(lambda runtime: any(
              option.startswith("no-new-privileges") and not option.endswith("false")
              for option in runtime.get("security_opt") or ())),
          "Without it, a setuid program inside the container can raise its own privileges.",
          "Add no-new-privileges:true to its security options."),
    Check("no-devices", "No host devices", _on(lambda runtime: not runtime.get("devices")),
          "A passed-through device is direct access to that hardware.",
          "Remove the device unless the service cannot work without it."),
    Check("own-network", "Its own network", _on(lambda runtime: runtime.get("network_mode") != "host"),
          "On the host network, every port it opens is the machine's, on every interface.",
          "Give it a bridge network and publish only the ports it serves."),
    Check("ports-bound", "Ports bound to an address", _on(_bound_to_an_address),
          "A port published on every interface is reachable from every network the machine is on.",
          "Publish it on the address it is meant to be reached at, 127.0.0.1 behind a proxy."),
    Check("memory-limited", "Memory limited", _on(lambda runtime: bool(runtime.get("memory_limit"))),
          "Without a limit, one runaway container can take the machine's memory from every other.",
          "Set a memory limit in the compose file."),
    Check("health-checked", "Declares a health check", _on(lambda runtime: bool(runtime.get("healthcheck"))),
          "Without one, only that it is running can be known, and an upgrade cannot be verified by it.",
          "Add a health check to the compose file."),
)


def posture_of(container: Any) -> Posture:
    return measure(container, STANDARD)
