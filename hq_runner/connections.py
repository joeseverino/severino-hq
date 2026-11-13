"""How the worker reaches a machine: the runner-role SSH connection that names
it, as the privilege profile a run needs, in the connections document the root
renderer writes.

A run names machines by their name in HQ, and each is reached through the
``ssh_transport`` connection whose role is ``runner`` and whose ``machine`` and
``profile`` are that machine and the run's profile: ``read`` for a procedure
that names no privilege, else its privilege. So a read and a change never
share a login. Any other connection in the document is another process's and
is never read. The identity and the pinned host key are the files the
renderer writes beside it (``<ssh dir>/<ref>`` and ``<ssh dir>/known_hosts``).
The document is read as the controller reads it
(``controller/connections/document.go:ReadFile``): this account's own private,
single-link regular file, of the one schema version both sides accept.
"""

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The connections document's schema version and read bound, as the
# controller's (controller/connections/document.go).
SCHEMA_VERSION = 2
MAX_BYTES = 1 << 20
ROLE = "runner"
# The profile of a login that escalates to nothing, as the controller's
# ReadProfile names it.
READ_PROFILE = "read"
KNOWN_HOSTS = "known_hosts"

# The keys of the document this reads; the contract test holds each to the
# schema HQ's registry emits.
DOCUMENT_KEYS = ("schema_version", "connections")
CONNECTION_KEYS = ("ref", "ssh_transport")
TRANSPORT_KEYS = ("host", "port", "user", "role", "machine", "profile")
# Read when a connection carries it: the shell a Windows host's sshd starts
# (powershell or cmd), which Ansible's ssh connection is told as its shell type.
SHELL_TYPE = "shell_type"

# As the controller and the renderer hold them (controller/runtime/connections.go):
# a host is a name or an address alone, the user a login name, and the ref a
# file name in the ssh directory.
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_PORT = re.compile(r"[0-9]{1,5}")
_SHELL_TYPES = frozenset({"sh", "powershell", "cmd"})
# A machine and a profile as the controller holds them (document.go).
_MACHINE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")
_PROFILE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


class ConnectionsError(Exception):
    """The connections document cannot be trusted or read."""


@dataclass(frozen=True, slots=True)
class Target:
    """One machine as the worker reaches it."""

    ref: str
    machine: str
    profile: str
    host: str
    port: int
    user: str
    identity: Path
    known_hosts: Path
    shell_type: str = ""


def _read(path: Path, uid: int) -> bytes:
    try:
        listed = os.lstat(path)
    except OSError as exc:
        raise ConnectionsError("the connections document is not readable") from exc
    if not stat.S_ISREG(listed.st_mode):
        raise ConnectionsError("the connections document is not a regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ConnectionsError("the connections document is not readable") from exc
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (listed.st_dev, listed.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ConnectionsError("the connections document is not a regular file")
        if opened.st_uid != uid or opened.st_nlink != 1:
            raise ConnectionsError(f"the connections document is not a single-link file owned by uid {uid}")
        if opened.st_mode & 0o077:
            raise ConnectionsError("the connections document is readable or writable by another account")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ConnectionsError("the connections document is too large")
    return data


def profile(privilege: str) -> str:
    """The profile of the login a procedure with ``privilege`` runs through:
    one that names none runs through the read login."""

    return privilege or READ_PROFILE


def _target(transport: Mapping[str, Any], ref: str, ssh_dir: Path) -> Target | None:
    host, port, user = transport.get("host"), transport.get("port"), transport.get("user")
    machine, named = transport.get("machine"), transport.get("profile")
    shell_type = transport.get(SHELL_TYPE, "")
    if not (isinstance(machine, str) and _MACHINE.fullmatch(machine)):
        return None
    if not (isinstance(named, str) and _PROFILE.fullmatch(named)):
        return None
    if not (isinstance(host, str) and _HOST.fullmatch(host)):
        return None
    if not (isinstance(user, str) and _USER.fullmatch(user)):
        return None
    if not (isinstance(port, str) and _PORT.fullmatch(port) and 1 <= int(port) <= 65535):
        return None
    if shell_type and shell_type not in _SHELL_TYPES:
        return None
    return Target(ref, machine, named, host, int(port), user, ssh_dir / ref, ssh_dir / KNOWN_HOSTS, shell_type)


type Reach = tuple[str, str]


def runner_targets(path: Path, ssh_dir: Path, *, uid: int | None = None) -> dict[Reach, Target]:
    """Each login a runner-role connection gives, by its machine and profile.

    A ref, or a machine and profile, two connections share reaches nothing,
    as the controller holds both, so which key answers never depends on
    order; a connection whose settings are not a usable SSH destination
    reaches nothing.
    """

    try:
        document = json.loads(_read(path, os.geteuid() if uid is None else uid))
    except ValueError as exc:
        raise ConnectionsError("the connections document is not JSON") from exc
    if not isinstance(document, Mapping) or document.get("schema_version") != SCHEMA_VERSION:
        raise ConnectionsError(f"the connections document is not schema version {SCHEMA_VERSION}")
    listed = document.get("connections")
    if not isinstance(listed, list):
        raise ConnectionsError("the connections document lists no connections")
    found: dict[str, Target | None] = {}
    shared: set[str] = set()
    for connection in listed:
        if not isinstance(connection, Mapping):
            continue
        ref = connection.get("ref")
        transport = connection.get("ssh_transport")
        if not isinstance(ref, str) or not _REF.fullmatch(ref) or ref == KNOWN_HOSTS:
            continue
        if ref in found:
            shared.add(ref)
        if isinstance(transport, Mapping) and transport.get("role") == ROLE:
            found[ref] = _target(transport, ref, ssh_dir)
        else:
            found.setdefault(ref, None)
    reached: dict[Reach, Target | None] = {}
    for ref, target in found.items():
        if target is None or ref in shared:
            continue
        reach = (target.machine, target.profile)
        reached[reach] = None if reach in reached else target
    return {reach: target for reach, target in reached.items() if target is not None}
