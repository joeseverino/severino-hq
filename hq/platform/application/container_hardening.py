"""The compose change that meets each "Runtime" check a container fails.

Derived from what HQ already read of the container (Docker's inspect,
``portainer.runtime``, its mounts) and written for its own compose service, so
the operator pastes it rather than composing it. A met check, or one HQ could
not read, gets no change; one that can break the container carries its
``caution``, and one HQ cannot write is an ``Unwritten`` naming what is missing.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.utils.text import get_text_list

from .container_standard import (
    POWERFUL_CAPABILITIES,
    ROOT_POSTURE,
    holds_socket,
    privileged_ports,
    root_ports,
    writable_system_binds,
)
from .finding_steps import Do
from .standards import INTENDED, Check, Standing
from .ui import ended
lazy from .upgrades import data_of

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


@dataclass(frozen=True, slots=True)
class Change:
    """Compose keys to set under its service, and what they can break."""

    checks: tuple[str, ...]
    key: str
    yaml: str
    caution: str = ""


@dataclass(frozen=True, slots=True)
class Removal:
    """Lines to delete from its service: a setting with no value that undoes it."""

    check: str
    lines: tuple[str, ...]
    caution: str = ""


@dataclass(frozen=True, slots=True)
class Unwritten:
    """A check HQ cannot write the change for, and why."""

    check: str
    reason: str


@dataclass(frozen=True, slots=True)
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

    def step(self, check: Check) -> Do | None:
        """Its fix for ``check`` as one step under that check's finding: the
        compose lines that meet it, with what they can break, or why HQ cannot
        write them."""

        where = self.compose_file or "its compose file"
        then = f"Then run docker compose up -d {self.service} in its directory."
        if changes := [change for change in self.changes if check.id in change.checks]:
            body = "\n".join(_indent(change.yaml, 4) for change in changes)
            cautions = tuple(change.caution for change in changes if change.caution)
            return Do(f"Set in {where}", value=f"services:\n  {self.service}:\n{body}", notes=(*cautions, then))
        for removal in self.removals:
            if removal.check == check.id:
                cautions = (removal.caution,) if removal.caution else ()
                return Do(f"Remove from {where}", value="\n".join(removal.lines), notes=(*cautions, then))
        for gap in self.unwritten:
            if gap.check == check.id:
                return Do(check.fix, notes=(gap.reason,))
        return None

    def covers(self, check_id: str) -> bool:
        """Whether HQ wrote a change (or a removal) that meets ``check_id``."""

        return any(check_id in change.checks for change in self.changes) or any(
            removal.check == check_id for removal in self.removals
        )


def hardening_of(container: Any) -> Hardening:
    """The compose change for every unmet check of ``container`` that one meets."""

    unmet = {result.check.id for result in container.posture.results if result.state == Standing.UNMET}
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


def _privileged(_container, _runtime, unmet):
    if "not-privileged" in unmet:
        yield Change(
            ("not-privileged",),
            "privileged",
            "privileged: false",
            "Anything it used privileged mode for, a device or a capability, then needs a line of its own.",
        )


def _process_namespace(_container, _runtime, unmet):
    if "own-process-namespace" in unmet:
        yield Removal("own-process-namespace", ("pid: host",))


def _security_options(_container, runtime, unmet):
    """``security_opt`` once, for both checks it can fail: what it keeps
    without the unconfined options, and no-new-privileges added."""

    wanted = {"confined", "no-new-privileges"} & unmet
    if not wanted:
        return
    options = [str(option) for option in runtime.get("security_opt") or ()]
    unconfined = [option for option in options if option.endswith(("=unconfined", ":unconfined"))]
    # Never empty: no-new-privileges is either kept, being met, or added.
    kept = [
        option
        for option in options
        if option not in unconfined and not ("no-new-privileges" in wanted and option.startswith("no-new-privileges"))
    ]
    if "no-new-privileges" in wanted:
        kept.append("no-new-privileges:true")
    cautions = []
    if "no-new-privileges" in wanted:
        cautions.append("A program that needs setuid to work (sudo, su) can no longer gain root.")
    if "confined" in wanted:
        cautions.append("System calls the default profile blocks now fail.")
    yield Change(tuple(sorted(wanted)), "security_opt", _listed("security_opt", kept), " ".join(cautions))


def _system_paths(container, _runtime, unmet):
    if "no-system-path-writable" not in unmet:
        return
    mounts = container.mounts or ()
    binds = [mount for mount in mounts if mount.get("type") == "bind"]
    offending = {id(mount) for mount in writable_system_binds(binds, docker_held=holds_socket(container))}
    lines = [
        f"{mount.get('source')}:{mount.get('destination')}"
        + (":ro" if mount.get("read_only") or id(mount) in offending else "")
        for mount in mounts
    ]
    yield Change(
        ("no-system-path-writable",),
        "volumes",
        _listed("volumes", lines),
        "Writes there fail, docker cp included, so a deploy that copies a file into that path breaks. "
        "This list replaces its volumes; every other mount is kept as it is.",
    )


def _capabilities(container, runtime, unmet):
    if "no-powerful-capability" not in unmet or ("not-root" in unmet and _must_be_root(container)):
        return
    added = [str(cap) for cap in runtime.get("cap_add") or ()]
    kept = [cap for cap in added if cap not in POWERFUL_CAPABILITIES]
    if privileged_ports(container) and "NET_BIND_SERVICE" not in kept:
        kept.append("NET_BIND_SERVICE")
    powerful = [cap for cap in added if cap in POWERFUL_CAPABILITIES]
    yaml = 'cap_drop:\n  - "ALL"\n' + (_listed("cap_add", kept) if kept else "cap_add: []")
    yield Change(
        ("no-powerful-capability",),
        "cap_drop",
        yaml,
        f"Drops {', '.join(powerful)}, and Docker's defaults with it: if it changes file owners, binds a port under 1024 "
        "or switches user, add CHOWN, NET_BIND_SERVICE or SETUID and SETGID back.",
    )


def _devices(_container, runtime, unmet):
    if "no-devices" in unmet:
        yield Removal(
            "no-devices",
            tuple(f"devices: {device}" for device in runtime.get("devices") or ()),
            "The hardware is no longer reachable from inside it.",
        )


def _network(_container, _runtime, unmet):
    if "own-network" in unmet:
        yield Removal(
            "own-network",
            ("network_mode: host",),
            "Docker reports no ports for a host-network container. "
            "Publish each port it serves under ports:, or nothing reaches it.",
        )


def _ports(_container, runtime, unmet):
    if "ports-bound" not in unmet:
        return
    lines = []
    for binding in runtime.get("port_bindings") or ():
        address = str(binding.get("host_ip", "") or "")
        address = "127.0.0.1" if address in ("", "0.0.0.0", "::") else address  # noqa: S104 - read from a binding to narrow it, never bound
        line = f"{address}:{binding.get('host_port', '')}:{_port(binding)}"
        if line not in lines:
            lines.append(line)
    yield Change(
        ("ports-bound",),
        "ports",
        _listed("ports", lines),
        "Only this machine can reach it on 127.0.0.1. If another machine reaches it directly, put the address it "
        "reaches it at in its place.",
    )


def _port(binding: Mapping[str, Any]) -> str:
    """``80`` for ``80/tcp``, ``53/udp`` as it is."""

    port = str(binding.get("container_port", "") or "")
    return port.removesuffix("/tcp")


def _memory(_container, runtime, unmet):
    if "memory-limited" not in unmet:
        return
    used = int(runtime.get("memory_usage") or 0)
    if used:
        limit = f"{max(64, -(-2 * used // (64 * MEBIBYTE)) * 64)}m"
        caution = f"Twice the {used // MEBIBYTE}m it was using when read, rounded up."
    else:
        limit = DEFAULT_MEMORY
        caution = (
            f"HQ has no reading of what it uses, so {DEFAULT_MEMORY} is a starting point: if it is killed "
            "for running out of memory, increase it."
        )
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
            "It publishes no port and HQ knows no health endpoint for its image. "
            "Add a healthcheck: whose test command fails when the service does.",
        )
        return
    url = f"http://127.0.0.1:{port}{path}"
    yield Change(
        ("health-checked",),
        "healthcheck",
        "healthcheck:\n"
        f'  test: ["CMD-SHELL", "wget -q --spider {url} || exit 1"]\n'
        "  interval: 30s\n  timeout: 5s\n  retries: 3",
        ended(f"Asks {why}" if why else f"Assumes port {port} answers HTTP at {path}")
        + " Needs wget in the image. With curl, use curl -fsS. With neither, use a check the image ships.",
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
    if _must_be_root(container):
        yield Change(
            ("not-root", *({"no-powerful-capability"} & unmet)),
            "cap_drop",
            ROOT_POSTURE,
            "It runs as root, which binding a port under 1024 on the host's network takes, with every other "
            "capability dropped: anything else it did as root (changing file owners, for one) then fails.",
        )
        return
    data = [str(mount.get("source")) for mount in data_of(container.mounts) if mount.get("type") == "bind"]
    caution = (
        f"Its files must be readable by {NON_ROOT} and its data writable: chown -R {NON_ROOT} {' '.join(data)} first."
        if data
        else "An image that expects root (installing or changing owners as it starts) refuses to run."
    )
    yaml = f'user: "{NON_ROOT}"'
    if low := privileged_ports(container):
        caution += (
            f" It binds {get_text_list(low, 'and')} on the host's network, which takes NET_BIND_SERVICE, "
            "and its image must grant that to its program."
        )
        added = [str(cap) for cap in runtime.get("cap_add") or ()]
        if "no-powerful-capability" not in unmet and "NET_BIND_SERVICE" not in added:
            yaml += "\n" + _listed("cap_add", [*added, "NET_BIND_SERVICE"])
    yield Change(("not-root",), "user", yaml, caution)


def _must_be_root(container) -> bool:
    return bool(root_ports(container, waived=container.posture.state_of("no-new-privileges") == INTENDED))


def _socket(_container, _runtime, unmet):
    if "no-docker-socket" in unmet:
        yield Unwritten(
            "no-docker-socket",
            "HQ cannot tell which Docker calls it needs, so it cannot write a socket proxy. "
            "If it is meant to control Docker, mark it, and HQ stops warning about it.",
        )


_BUILDERS = (
    _privileged,
    _socket,
    _process_namespace,
    _security_options,
    _system_paths,
    _capabilities,
    _user,
    _devices,
    _network,
    _ports,
    _memory,
    _health,
)
