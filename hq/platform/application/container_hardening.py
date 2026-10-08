"""The compose change that meets each "How it runs" check a container fails.

Derived from what HQ already read of the container (Docker's inspect of it,
``portainer.runtime``, and its mounts) and written for its own compose service,
so the operator pastes it rather than composing it. Only for a check that is
not met: a met check, or one HQ could not read, is never given a change.

A change that can break the container says so beside itself: a non-root user
needs its files readable, a read-only mount refuses every write (``docker cp``
into it included), dropping capabilities takes the ones it quietly relied on.
What HQ cannot write, because the answer is not in anything it read, is said
as a reason instead, naming what is missing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .container_standard import POWERFUL_CAPABILITIES, holds_socket, writable_system_binds
from .standards import UNMET

# Where HQ has no reading of what it uses: enough for most small services,
# said as a starting point rather than a measurement.
DEFAULT_MEMORY = "512m"
MEBIBYTE = 1024 * 1024
# Health endpoints images document, by the image as people write it:
# (container port, path).
KNOWN_HEALTH = {
    "grafana/grafana": ("3000", "/api/health"),
    "prom/prometheus": ("9090", "/-/healthy"),
    "prom/alertmanager": ("9093", "/-/healthy"),
}
# The user a derived ``user:`` names: the first ordinary user on most machines.
NON_ROOT = "1000:1000"


@dataclass(frozen=True)
class Change:
    """Compose keys to set under its service, and what they can break."""

    checks: tuple[str, ...]
    key: str
    yaml: str
    caution: str = ""


@dataclass(frozen=True)
class Removal:
    """Lines to delete from its service: a setting with no value that undoes it."""

    check: str
    lines: tuple[str, ...]
    caution: str = ""


@dataclass(frozen=True)
class Unwritten:
    """A check HQ cannot write the change for, and why."""

    check: str
    reason: str


@dataclass(frozen=True)
class Hardening:
    container: Any
    service: str
    changes: tuple[Change, ...] = ()
    removals: tuple[Removal, ...] = ()
    unwritten: tuple[Unwritten, ...] = ()

    @property
    def compose_file(self) -> str:
        files = self.container.compose_files
        return files[0] if files else ""

    @property
    def yaml(self) -> str:
        """Every change, under its service, as one block to paste."""

        if not self.changes:
            return ""
        body = "\n".join(_indent(change.yaml, 4) for change in self.changes)
        return f"services:\n  {self.service}:\n{body}\n"

    @property
    def cautions(self) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.caution)

    @property
    def any(self) -> bool:
        return bool(self.changes or self.removals or self.unwritten)

    @property
    def socket_link(self) -> Any:
        """The update that marks it as holding the socket, when HQ watches it
        and the socket is what it fails."""

        if not self.container.running.watcher or not any(item.check == "no-docker-socket" for item in self.unwritten):
            return None
        from .container_attention import socket_holder_link

        return socket_holder_link(self.container)

    def covers(self, check_id: str) -> bool:
        """Whether HQ wrote a change (or a removal) that meets ``check_id``."""

        return any(check_id in change.checks for change in self.changes) or any(
            removal.check == check_id for removal in self.removals
        )


def hardening_of(container: Any) -> Hardening:
    """The compose change for every unmet check of ``container`` that one meets."""

    unmet = {result.check.id for result in container.posture.results if result.state == UNMET}
    runtime = container.runtime or {}
    service = str(runtime.get("service", "") or "") or container.running.name
    changes: list[Change] = []
    removals: list[Removal] = []
    unwritten: list[Unwritten] = []
    for build in _BUILDERS:
        for found in build(container, runtime, unmet):
            if isinstance(found, Change):
                changes.append(found)
            elif isinstance(found, Removal):
                removals.append(found)
            else:
                unwritten.append(found)
    return Hardening(container, service, tuple(changes), tuple(removals), tuple(unwritten))


def _indent(text: str, width: int) -> str:
    return "\n".join(" " * width + line for line in text.splitlines())


def _listed(key: str, values) -> str:
    return f"{key}:\n" + "\n".join(f'  - "{value}"' for value in values)


def _privileged(container, runtime, unmet):
    if "not-privileged" in unmet:
        yield Change(
            ("not-privileged",), "privileged", "privileged: false",
            "Anything it used privileged mode for, a device or a capability, then needs a line of its own.",
        )


def _process_namespace(container, runtime, unmet):
    if "own-process-namespace" in unmet:
        yield Removal("own-process-namespace", ("pid: host",))


def _security_options(container, runtime, unmet):
    """``security_opt`` once, for both checks it can fail: what it keeps
    without the unconfined options, and no-new-privileges added."""

    wanted = {"confined", "no-new-privileges"} & unmet
    if not wanted:
        return
    options = [str(option) for option in runtime.get("security_opt") or ()]
    unconfined = [option for option in options if option.endswith(("=unconfined", ":unconfined"))]
    # Never empty: no-new-privileges is either kept, being met, or added.
    kept = [
        option for option in options
        if option not in unconfined and not ("no-new-privileges" in wanted and option.startswith("no-new-privileges"))
    ]
    if "no-new-privileges" in wanted:
        kept.append("no-new-privileges:true")
    cautions = []
    if "no-new-privileges" in wanted:
        cautions.append("A program that needs setuid to work (sudo, su) can no longer gain root.")
    if "confined" in wanted:
        cautions.append("A system call the default profile refuses now fails, which is what the profile is for.")
    yield Change(tuple(sorted(wanted)), "security_opt", _listed("security_opt", kept), " ".join(cautions))


def _system_paths(container, runtime, unmet):
    if "no-system-path-writable" not in unmet:
        return
    mounts = container.mounts or ()
    binds = [mount for mount in mounts if mount.get("type") == "bind"]
    offending = {id(mount) for mount in writable_system_binds(binds, holds_socket(container))}
    lines = [
        f"{mount.get('source')}:{mount.get('destination')}"
        + (":ro" if mount.get("read_only") or id(mount) in offending else "")
        for mount in mounts
    ]
    yield Change(
        ("no-system-path-writable",), "volumes", _listed("volumes", lines),
        "Read-only refuses every write there, and docker cp into it fails too: a deploy that copies a file into "
        "that path stops working. This list replaces its volumes, so every other mount is written as it is.",
    )


def _capabilities(container, runtime, unmet):
    if "no-powerful-capability" not in unmet:
        return
    added = [str(cap) for cap in runtime.get("cap_add") or ()]
    kept = [cap for cap in added if cap not in POWERFUL_CAPABILITIES]
    powerful = [cap for cap in added if cap in POWERFUL_CAPABILITIES]
    yaml = 'cap_drop:\n  - "ALL"\n' + (_listed("cap_add", kept) if kept else "cap_add: []")
    yield Change(
        ("no-powerful-capability",), "cap_drop", yaml,
        f"Drops {', '.join(powerful)}, and Docker's defaults with it: if it changes file owners, binds a port under 1024 "
        "or switches user, add CHOWN, NET_BIND_SERVICE or SETUID and SETGID back.",
    )


def _devices(container, runtime, unmet):
    if "no-devices" in unmet:
        yield Removal(
            "no-devices", tuple(f"devices: {device}" for device in runtime.get("devices") or ()),
            "The hardware is no longer reachable from inside it.",
        )


def _network(container, runtime, unmet):
    if "own-network" in unmet:
        yield Removal(
            "own-network", ("network_mode: host",),
            "Docker reports no ports for a container on the host network, so which it serves is not known here: "
            "publish each under ports:, or nothing reaches it.",
        )


def _ports(container, runtime, unmet):
    if "ports-bound" not in unmet:
        return
    lines = []
    for binding in runtime.get("port_bindings") or ():
        address = str(binding.get("host_ip", "") or "")
        address = "127.0.0.1" if address in ("", "0.0.0.0", "::") else address
        line = f"{address}:{binding.get('host_port', '')}:{_port(binding)}"
        if line not in lines:
            lines.append(line)
    yield Change(
        ("ports-bound",), "ports", _listed("ports", lines),
        "Only this machine can reach it on 127.0.0.1. If another machine reaches it directly, put the address it "
        "reaches it at in its place.",
    )


def _port(binding: Mapping[str, Any]) -> str:
    """``80`` for ``80/tcp``, ``53/udp`` as it is."""

    port = str(binding.get("container_port", "") or "")
    return port.removesuffix("/tcp")


def _memory(container, runtime, unmet):
    if "memory-limited" not in unmet:
        return
    used = int(runtime.get("memory_usage") or 0)
    if used:
        limit = f"{max(64, -(-2 * used // (64 * MEBIBYTE)) * 64)}m"
        caution = f"Twice the {used // MEBIBYTE}m it was using when read, rounded up."
    else:
        limit = DEFAULT_MEMORY
        caution = (f"HQ has no reading of what it uses, so {DEFAULT_MEMORY} is a starting point: if it is killed "
                   "for running out of memory, increase it.")
    yield Change(("memory-limited",), "mem_limit", f"mem_limit: {limit}", caution)


def _health(container, runtime, unmet):
    if "health-checked" not in unmet:
        return
    known = KNOWN_HEALTH.get(container.standing.image.short)
    if known:
        port, path = known
        why = "the health endpoint its image documents"
    else:
        port, path = _served_port(runtime)
        why = ""
    if not port:
        yield Unwritten(
            "health-checked",
            "It publishes no port and HQ knows no health endpoint for its image, so what proves it works is yours "
            "to name: a healthcheck: whose test is a command that fails when it does.",
        )
        return
    url = f"http://127.0.0.1:{port}{path}"
    yield Change(
        ("health-checked",), "healthcheck",
        "healthcheck:\n"
        f'  test: ["CMD-SHELL", "wget -q --spider {url} || exit 1"]\n'
        "  interval: 30s\n  timeout: 5s\n  retries: 3",
        (f"Asks {why}. " if why else f"Assumes port {port} answers HTTP at {path}. ")
        + "Needs wget in the image: with curl instead, use curl -fsS; an image with neither needs a check it ships.",
    )


def _served_port(runtime: Mapping[str, Any]) -> tuple[str, str]:
    for binding in runtime.get("port_bindings") or ():
        port = _port(binding)
        if port.isdigit():
            return port, "/"
    return "", ""


def _user(container, runtime, unmet):
    if "not-root" not in unmet:
        return
    from .upgrades import data_of

    data = [str(mount.get("source")) for mount in data_of(container.mounts) if mount.get("type") == "bind"]
    low = sorted({port for binding in runtime.get("port_bindings") or () if (port := _port(binding)).isdigit() and int(port) < 1024})
    caution = (
        f"Its files must be readable by {NON_ROOT} and its data writable: chown -R {NON_ROOT} {' '.join(data)} first."
        if data else "An image that expects root (installing or changing owners as it starts) refuses to run."
    )
    if low:
        caution += f" It listens on {', '.join(low)}, which only root binds without NET_BIND_SERVICE."
    yield Change(("not-root",), "user", f'user: "{NON_ROOT}"', caution)


def _socket(container, runtime, unmet):
    if "no-docker-socket" in unmet:
        yield Unwritten(
            "no-docker-socket",
            "HQ cannot tell which Docker calls it needs, so it cannot write a socket proxy for it. "
            "If this container is meant to control Docker, say so and HQ stops warning about it.",
        )


_BUILDERS = (
    _privileged, _socket, _process_namespace, _security_options, _system_paths, _capabilities,
    _user, _devices, _network, _ports, _memory, _health,
)
