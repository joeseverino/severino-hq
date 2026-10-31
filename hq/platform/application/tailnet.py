"""Whether one machine on the tailnet may reach another, and on which port.

HQ does not evaluate the policy: Tailscale does, during the sweep, and HQ only
checks set membership against its answer, since a second implementation of an
access policy would be believed as much as the real one and wrong unnoticed. So
a verdict is what the policy says, never what Tailscale did, and a question the
sweep cannot answer says so rather than guessing.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_KIND, TAILNET_POLICY_KIND
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations.host import TAILNET_LOCK_KIND
lazy from hq.domains.control_plane.observations.hq import ARRIVAL_KIND

from .projection import projection_scope, read_once
from .ui import counted, ended
lazy from . import connections
lazy from .connections import connection_rows
lazy from .facts import inventory_records
lazy from .hq_self import scoped_served_port, site_label
lazy from .infrastructure import enabled_resources
lazy from .locate import host_of, points_at_host, split_endpoint

# The settings row naming the tailnet's resolvers, by address.
RESOLVES_THROUGH = "Resolves through"
# The setting row that says whether a new device waits for approval.
NEW_DEVICES = "New devices"


@dataclass(frozen=True, slots=True)
class Device:
    """One machine as the policy names it: an owner, some tags, some ports."""

    name: str
    user: str = ""
    tags: tuple[str, ...] = ()
    reach: dict[int, tuple[str, ...]] = field(default_factory=dict)
    rules: dict[int, tuple[dict, ...]] = field(default_factory=dict)
    addresses: tuple[str, ...] = ()
    # Names the policy gives this device's addresses. Not a property of the
    # device at all (the policy decides it) so it is attached when the two
    # readings are joined rather than read from the device.
    aliases: tuple[str, ...] = ()
    dns_name: str = ""
    os: str = ""
    online: bool = False
    observer: bool = False
    # The link itself (path, handshake, traffic) is ``machines.Presence``'s.
    endpoints: tuple[str, ...] = ()
    key_expires: str = ""
    # The WireGuard public key. A peering is not a claim in an inventory, it is
    # two keys that have completed a handshake, and this is the half of that
    # pair a page can show.
    public_key: str = ""
    offers_exit_node: bool = False
    # What actually admits a node, as distinct from what lists one. A device in
    # the inventory has been seen; a device the tailnet has authorized and lock
    # has signed is one the tailnet will carry traffic for.
    authorized: bool = True
    lock_error: str = ""
    observed_at: datetime | None = None

    @property
    def label(self) -> str:
        """The name worth showing a person for this device.

        A node registers under whatever its operating system calls itself, and
        several of them call themselves the same unhelpful thing: a phone
        reporting "localhost" is not a bug in the sweep, it is the hostname.
        The MagicDNS label is the tailnet's own name for the node and is unique
        within it by construction, so it wins wherever the two disagree.
        """

        magic = self.dns_name.partition(".")[0]
        return magic or self.name

    @property
    def principals(self) -> frozenset[str]:
        """Every name a rule could admit this device by: owner, tag or alias.

        A policy naming an alias of one of the device's addresses admits the
        machine rather than whoever is signed in on it, so counting only owners
        and tags reports a device the policy plainly admits as refused.
        """

        return frozenset({self.user, *self.tags, *self.aliases} - {""})

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(sorted(self.reach))

    @property
    def openings(self) -> tuple[tuple[int, tuple[str, ...]], ...]:
        """``(port, who)`` in port order, for a template that cannot index."""

        return tuple((port, self.reach[port]) for port in self.ports)


@dataclass(frozen=True, slots=True, kw_only=True)
class Verdict:
    """One answer, and the reason it is that answer."""

    allowed: bool
    known: bool
    detail: str
    via: tuple[str, ...] = ()
    # The rules that decided it, so the answer shows its own reasoning and an
    # operator can go and change the thing that produced it.
    rules: tuple[dict, ...] = ()

    @property
    def label(self) -> str:
        if not self.known:
            return "Cannot say"
        return "Allowed" if self.allowed else "Not allowed"


def devices() -> dict[str, Device]:
    """Every device the last sweep described, by the name the tailnet uses."""

    # Both readings, from the one query that already fetches them together.
    # The policy names addresses and the device reading names devices; only
    # joined do they say which aliases admit which machine. Read through
    # `policy()` instead, this would be a second identical query on every
    # surface that is not inside a projection scope.
    both = snapshots()
    aliases_at: dict[str, list[str]] = {}
    for snapshot in both[POLICY_KIND]:
        for record in snapshot.records:
            for alias, address in (record.get("hosts") or {}).items():
                if alias and address:
                    aliases_at.setdefault(str(address), []).append(str(alias))

    found: dict[str, Device] = {}
    for snapshot in both[TAILNET_KIND]:
        for record in snapshot.records:
            name = str(record.get("name", ""))
            if not name:
                continue
            found[name] = Device(
                name=name,
                user=str(record.get("user", "")),
                tags=tuple(str(tag) for tag in record.get("tags") or ()),
                reach={
                    int(entry["port"]): tuple(entry.get("who") or ())
                    for entry in record.get("reach") or ()
                    if str(entry.get("port", "")).isdigit()
                },
                rules={
                    int(entry["port"]): tuple(entry.get("rules") or ())
                    for entry in record.get("reach") or ()
                    if str(entry.get("port", "")).isdigit()
                },
                addresses=tuple(str(address) for address in record.get("addresses") or ()),
                aliases=tuple(
                    sorted(
                        {
                            alias
                            for address in record.get("addresses") or ()
                            for alias in aliases_at.get(str(address), ())
                        }
                    )
                ),
                dns_name=str(record.get("dns_name", "")),
                os=str(record.get("os", "")),
                online=bool(record.get("online")),
                observer=bool(record.get("self")),
                public_key=str(record.get("public_key", "")),
                authorized=bool(record.get("authorized", True)),
                lock_error=str(record.get("lock_error", "")),
                endpoints=tuple(str(endpoint) for endpoint in record.get("endpoints") or ()),
                key_expires=str(record.get("key_expires", "")),
                offers_exit_node=bool(record.get("offers_exit_node")),
                observed_at=snapshot.observed_at,
            )
    return found


def device_at(address: str, known: dict[str, Device] | None = None) -> Device | None:
    """The device answering at an address, where the tailnet knows of one.

    ``known`` lets a caller that already read the inventory hand it in. One
    surface asks three of these questions about the same sweep, and reading it
    once per question is three identical queries for one answer.
    """

    wanted = str(address or "").strip()
    if not wanted:
        return None
    return next(
        (found for found in (known if known is not None else devices()).values() if wanted in found.addresses),
        None,
    )


def observer(known: dict[str, Device] | None = None) -> Device | None:
    """The device whose daemon took the reading: the one HQ runs on."""

    return next(
        (found for found in (known if known is not None else devices()).values() if found.observer),
        None,
    )


def alias_owners(known: dict[str, Device] | None = None) -> dict[str, str]:
    """Each policy alias a device carries, mapped to that device's label.

    A device with an IPv4 and an IPv6 address is usually named twice in the
    policy, once per address; both become the one device a person knows.
    """

    known = devices() if known is None else known
    return {alias: device.label for device in known.values() for alias in device.aliases}


def as_devices(names, owners: dict[str, str]) -> tuple[str, ...]:
    """Policy names, with host aliases replaced by their device, each once."""

    return tuple(dict.fromkeys(owners.get(name, name) for name in names))


def by_device(destinations, owners: dict[str, str]) -> tuple[str, ...]:
    """``alias:port`` destinations, as one ``device: ports`` entry per device."""

    ports_of: dict[str, set[str]] = {}
    for destination in destinations:
        name, _, port = str(destination).rpartition(":")
        # Only a port after the colon makes it one: "autogroup:internet" is a name.
        if not name or not _PORT.fullmatch(port):
            name, port = str(destination), ""
        ports_of.setdefault(owners.get(name, name), set()).add(port)
    return tuple(
        f"{device}: {', '.join(sorted((p for p in ports if p), key=_port_order))}" if any(ports) else device
        for device, ports in ports_of.items()
    )


_PORT = re.compile(r"\*|[0-9]+(?:-[0-9]+)?")


def _port_order(port: str) -> tuple[int, str]:
    """Numeric order for ``443``, ``tcp:443``; anything else after."""

    match = _PORT_ENTRY.fullmatch(str(port))
    return (int(match.group(1)), str(port)) if match else (1 << 16, str(port))


def ports() -> tuple[int, ...]:
    """Every port the sweep asked about, so the form offers exactly those."""

    return tuple(sorted({port for device in devices().values() for port in device.reach}))


def may_reach(source: str, target: str, port: int, known: dict[str, Device] | None = None) -> Verdict:
    """Whether the policy admits ``source`` to ``target`` on ``port``.

    Three answers, not two. "Cannot say" is a real outcome and is kept distinct
    from "not allowed": a device nothing has swept, or a port nobody asked
    about, is a gap in what HQ was told rather than a decision the policy made.
    """

    known = devices() if known is None else known
    who_asks = known.get(source)
    who_answers = known.get(target)
    if who_asks is None or who_answers is None:
        missing = source if who_asks is None else target
        return Verdict(allowed=False, known=False, detail=f"{missing} was not in the last read of the tailnet.")
    if not who_asks.principals:
        return Verdict(
            allowed=False,
            known=False,
            detail=f"{source} has no user or tag, so no rule can match it.",
        )
    admitted = who_answers.reach.get(port)
    if admitted is None:
        return Verdict(
            allowed=False,
            known=False,
            detail=f"Port {port} on {target} was not checked.",
        )
    # The sweep stored Tailscale's own answer for each device and port, with
    # groups already flattened to the users in them, so this is set membership.
    matched = tuple(sorted(who_asks.principals & set(admitted)))
    owners = alias_owners(known)
    if matched:
        return Verdict(
            allowed=True,
            known=True,
            detail=ended(
                f"Allowed: {', '.join(as_devices(matched, owners))} to {owners.get(target, target)} on {port}"
            ),
            via=matched,
            rules=who_answers.rules.get(port, ()),
        )
    return Verdict(
        allowed=False,
        known=True,
        detail=f"No rule allows {', '.join(as_devices(sorted(who_asks.principals), owners))} "
        f"to {owners.get(target, target)} on {port:d}. Allowed: "
        f"{ended(', '.join(as_devices(admitted, owners)))}",
        rules=who_answers.rules.get(port, ()),
    )


POLICY_KIND = TAILNET_POLICY_KIND


@dataclass(frozen=True, slots=True)
class Policy:
    """The policy as HQ last read it, in the shape a page renders."""

    groups: tuple[dict, ...] = ()
    tags: tuple[dict, ...] = ()
    grants: tuple[dict, ...] = ()
    tests: tuple[dict, ...] = ()
    settings: dict = field(default_factory=dict)
    # Alias -> address, as the policy defines them.
    hosts: dict = field(default_factory=dict)
    dns: dict = field(default_factory=dict)
    # The controller's machine's own reading of tailnet lock, which the API
    # the rest is read from does not hold.
    lock: dict = field(default_factory=dict)
    services: tuple[dict, ...] = ()
    app_connectors: tuple[dict, ...] = ()
    ssh_rules: tuple[dict, ...] = ()
    # Access rules in the older ``acls`` form, which a policy may still use
    # instead of grants or beside them.
    acls: tuple[dict, ...] = ()
    # When the policy was read; None for the empty one nothing swept.
    observed_at: datetime | None = None

    @property
    def facts(self) -> tuple[tuple[str, str], ...]:
        """The tailnet's own settings, in the order they matter to an operator.

        Read rather than declared, and phrased as what is true rather than as
        a field name: ``devicesKeyDurationDays: 180`` is a number, and "keys
        last 180 days" is the thing somebody came to find out.
        """

        rows: list[tuple[str, str]] = []
        nameservers = self.dns.get("dns") or []
        if nameservers:
            rows.append((RESOLVES_THROUGH, ", ".join(nameservers)))
        rows.append(("MagicDNS", "On" if self.dns.get("magicDNS") else "Off"))
        days = self.settings.get("devicesKeyDurationDays")
        if days:
            rows.append(("Device keys expire after", f"{days} days"))
        rows.append(
            (
                NEW_DEVICES,
                "Need approval" if self.settings.get("devicesApprovalOn") else "Join without approval",
            )
        )
        rows.append(
            (
                "New users",
                "Need approval" if self.settings.get("usersApprovalOn") else "Join without approval",
            )
        )
        rows.append(
            (
                "Client updates",
                "Automatic" if self.settings.get("devicesAutoUpdatesOn") else "Manual",
            )
        )
        rows.append(
            (
                "Policy edited in",
                "Outside Tailscale" if self.settings.get("aclsExternallyManagedOn") else "Tailscale",
            )
        )
        # Only when the reading exists. A tailnet whose daemon never answered
        # should say nothing here rather than report lock as off, which is a
        # claim about the tailnet rather than about the reading.
        if self.lock:
            keys = self.lock.get("trusted_keys") or 0
            rows.append(
                (
                    "Tailnet lock",
                    (f"On · {counted(keys, 'signing key', 'signing keys')}" if self.lock.get("enabled") else "Off"),
                )
            )
        return tuple(rows)

    @property
    def locked_out(self) -> tuple[str, ...]:
        """Machines tailnet lock is currently filtering out of the tailnet."""

        return tuple(self.lock.get("locked_out") or ())

    @property
    def known(self) -> bool:
        return bool(self.groups or self.tags or self.grants)


def declaration() -> str:
    """The key of the policy declaration, when one has been adopted.

    What turns every rule on the page from something to read into something to
    change: with it, each row can point at the form that edits the document it
    came from.
    """

    # From the read every estate page already shares, in its order.
    return next((resource.key for resource in enabled_resources() if resource.kind == POLICY_KIND), "")


def proposed_grant(source: str, target: str, port: int) -> dict:
    """The grant that would allow a thing the policy currently refuses.

    Offered rather than applied. It is written in the terms the policy already
    uses: a tag where the machine carries one, the owner where it does not,
    because a grant naming a raw address would work once and then be wrong the
    first time an address moved.
    """

    known = devices()
    who = known.get(source)
    what = known.get(target)
    if who is None or what is None:
        return {}
    return {
        "src": [who.tags[0] if who.tags else who.user],
        "dst": [what.tags[0] if what.tags else what.user],
        "ip": [f"tcp:{port}"],
    }


def snapshots() -> dict[str, list]:
    """The tailnet readings, and HQ's record of which devices reached it, in
    one query, once per projection.

    They live in the same table under four kinds, and the dashboard wants
    them together: what each machine reports, the policy, what lock is
    filtering out, and when each device last reached HQ. Read together they
    are one query, and every caller inside the projection shares it.
    """

    def load() -> dict[str, list]:
        kinds = (TAILNET_KIND, POLICY_KIND, TAILNET_LOCK_KIND, ARRIVAL_KIND)
        found: dict[str, list] = {kind: [] for kind in kinds}
        for snapshot in ProviderInventory.objects.filter(kind__in=kinds):
            found.setdefault(snapshot.kind, []).append(snapshot)
        return found

    return read_once("tailnet.snapshots", load)


def policy() -> Policy:
    """What the tailnet's policy says, or an empty one if nothing swept it,
    with the last lock reading either way."""

    found = snapshots()
    lock = next((dict(record) for snapshot in found[TAILNET_LOCK_KIND] for record in snapshot.records), {})
    for snapshot in found[POLICY_KIND]:
        for record in snapshot.records:
            document = _document(record)
            return Policy(
                groups=tuple(record.get("groups") or _named(document.get("groups"), "members")),
                tags=tuple(record.get("tags") or _named(document.get("tagOwners"), "owners")),
                grants=tuple(record.get("grants") or document.get("grants") or ()),
                tests=tuple(record.get("tests") or document.get("tests") or ()),
                acls=tuple(rule for rule in document.get("acls") or () if isinstance(rule, dict)),
                settings=record.get("settings") or {},
                hosts=record.get("hosts") or {},
                dns=record.get("dns") or {},
                lock=lock,
                services=tuple(record.get("services") or ()),
                app_connectors=tuple(record.get("app_connectors") or ()),
                ssh_rules=tuple(record.get("ssh_rules") or ()),
                observed_at=snapshot.observed_at,
            )
    return Policy(lock=lock)


def _document(record) -> dict:
    """The policy document a reading carries, parsed, or {} when it cannot be.

    The parsed lists in a reading are what the controller chose to extract;
    the document is the whole policy. A summary built from the lists alone
    would read "0 grants" for a policy written as ``acls``, or for a reading
    whose lists were never extracted.
    """

    try:
        parsed = json.loads(str(record.get("document") or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _named(mapping, field: str) -> tuple[dict, ...]:
    """``{"group:a": [...]}`` as the reading's ``[{"name", field}]`` rows."""

    if not isinstance(mapping, dict):
        return ()
    return tuple({"name": name, field: sorted(values or ())} for name, values in sorted(mapping.items()))


def policy_allowing(document: str, *, source: str, target: str, port: int) -> tuple[str, str]:
    """The same policy with one path opened, and a sentence saying what moved.

    Returns ``("", "")`` when the policy already admits the path, so a caller
    proposes nothing rather than an empty change. A policy test denying the path
    moves with the grant, or Tailscale would refuse the whole document.
    """

    parsed = json.loads(document)
    granted = _grant(parsed, source, target, f"tcp:{port}")
    if not granted:
        return "", ""
    asserted = f"{target}:{port}"
    tests = [dict(test) for test in (parsed.get("tests") or ())]
    moved: list[str] = []
    # A policy carries its own tests, and Tailscale runs them before accepting
    # a policy, refusing the whole document when one fails. A test asserting
    # this source may not reach this target on this port contradicts the grant,
    # so the grant alone would be rejected in full: the assertion moves with it.
    for test in tests:
        if _denies(test, source, asserted):
            _accept(test, asserted)
            moved.append(f"{asserted} moved from denied to accepted for {source}")
    parsed["tests"] = tests
    return json.dumps(parsed), "; ".join((granted, *moved))


def _grant(parsed: dict[str, Any], source: str, target: str, wanted: str) -> str:
    """Open ``wanted`` from ``source`` to ``target`` in the policy's grants, on
    the grant between the two when there is one, and say what changed; ``""``
    when that grant already carries it."""

    grants = list(parsed.get("grants") or [])
    existing = next(
        (grant for grant in grants if source in (grant.get("src") or ()) and target in (grant.get("dst") or ())),
        None,
    )
    if existing is None:
        parsed["grants"] = [*grants, {"src": [source], "dst": [target], "ip": [wanted]}]
        return f"a grant from {source} to {target} on {wanted}"
    ports = list(existing.get("ip") or ())
    if wanted in ports:
        return ""
    # Appended rather than inserted: a reviewer reads the diff of the most
    # dangerous document in the estate, and an insertion renumbers every
    # element after it, so one added port would render as several changed lines.
    existing["ip"] = [*ports, wanted]
    parsed["grants"] = grants
    return f"{wanted} added to the grant from {source} to {target}"


def _denies(test: dict[str, Any], source: str, asserted: str) -> bool:
    """Whether a policy test asserts that ``source`` may not reach ``asserted`` over TCP."""

    return test.get("src") == source and test.get("proto", "tcp") == "tcp" and asserted in (test.get("deny") or ())


def _accept(test: dict[str, Any], asserted: str) -> None:
    """Move ``asserted`` from the test's denials to its acceptances, dropping
    a denial list it leaves empty."""

    denied = list(test["deny"])
    denied.remove(asserted)
    if denied:
        test["deny"] = denied
    else:
        test.pop("deny", None)
    accepted = list(test.get("accept") or ())
    if asserted not in accepted:
        test["accept"] = [*accepted, asserted]


# Names for ports no connection or container on the destination explains.
WELL_KNOWN_PORTS = {
    22: "SSH",
    53: "DNS",
    80: "HTTP",
    443: "HTTPS",
    3389: "RDP",
    5432: "PostgreSQL",
}
_PORT_ENTRY = re.compile(r"(?:(?:tcp|udp|sctp):)?([0-9]+)")


def grant_ports(grants, known: dict[str, Device] | None = None, machines=None) -> tuple[dict, ...]:
    """Each grant with ``ports``: ``(entry, name)`` per ``ip`` entry.

    A single port is named, in order, by HQ itself when the destination runs HQ
    on that port, an SSH connection to a destination machine on that port, a
    container publishing it there, then ``WELL_KNOWN_PORTS``. Otherwise the
    name is blank.
    """

    with projection_scope():
        return _grant_ports(grants, known, machines)


def _grant_ports(grants, known, machines) -> tuple[dict, ...]:
    known = devices() if known is None else known
    machines = connections.machines_once() if machines is None else machines
    by_name: dict[str, object] = {}
    for item in machines:
        for name in (item.name, *item.aliases):
            by_name.setdefault(str(name).lower(), item)
    namer = _PortNamer(_ssh_ports(), _service_ports())
    shown = []
    for grant in grants:
        targets = _grant_machines(grant.get("dst") or (), known, by_name)
        ports = []
        for entry in grant.get("ip") or ():
            match = _PORT_ENTRY.fullmatch(str(entry))
            ports.append((entry, namer.name(int(match.group(1)), targets) if match else ""))
        shown.append({**grant, "ports": tuple(sorted(ports, key=lambda port: _port_order(port[0])))})
    return tuple(shown)


def _ssh_ports() -> dict[str, set[int]]:
    """The port each SSH-shaped connection opens, by connection ref."""

    found: dict[str, set[int]] = {}
    for row in connection_rows():
        if not points_at_host(row.endpoint):
            continue
        port = split_endpoint(row.endpoint)[1]
        if port.isdigit():
            found.setdefault(row.connection_ref, set()).add(int(port))
    return found


@dataclass(frozen=True, slots=True)
class _ServicePort:
    """A port a connection to a service names, and where that connection points."""

    ref: str
    controller: str
    host: str
    port: int
    label: str


def _service_ports() -> tuple[_ServicePort, ...]:
    """The port each connection to a service names in its URL, with the service's name."""

    found = []
    for row in connection_rows():
        label = CONNECTION_LABELS.get(row.provider, "")
        if not label or points_at_host(row.endpoint):
            continue
        host, port = split_endpoint(row.endpoint)
        if host and port.isdigit():
            found.append(_ServicePort(row.connection_ref, row.controller_id, host, int(port), label))
    return tuple(found)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _grant_machines(names, known, by_name) -> tuple:
    """The machines a grant's destinations name, through their tailnet devices."""

    found = []
    for device in known.values():
        if not any(name == "*" or name in device.principals or name in device.addresses for name in names):
            continue
        machine = by_name.get(device.label.lower()) or by_name.get(device.name.lower())
        if machine is not None and machine not in found:
            found.append(machine)
    return tuple(found)


class _PortNamer:
    """What answers on a port of a grant's machines: HQ, SSH, a container, a service."""

    def __init__(self, ssh_ports: dict[str, set[int]], service_ports: tuple[_ServicePort, ...] = ()):
        self.ssh_ports = ssh_ports
        self.service_ports = service_ports
        self.hq_port = scoped_served_port()

    def name(self, port: int, targets) -> str:
        if port == self.hq_port and any(target.runs_hq for target in targets):
            return site_label()
        if any(self._opens(target, port) for target in targets):
            return CONNECTION_LABELS["ssh"]
        for target in targets:
            for running in sorted(target.containers, key=lambda item: item.name):
                if port in running.ports:
                    return running.name
        for target in targets:
            label = self._service(target, port)
            if label:
                return label
        return WELL_KNOWN_PORTS.get(port, "")

    def _service(self, target, port: int) -> str:
        """The service a connection reads on this port of this machine."""

        refs = {*target.reached_by, *target.opened_by}
        names = {str(name).lower() for name in (target.name, *target.aliases)}
        addresses = {host_of(address) for address in target.addresses}
        for item in self.service_ports:
            if item.port != port:
                continue
            here = _is_loopback(item.host) and item.controller.lower() in names
            if here or item.ref in refs or item.host in addresses:
                return item.label
        return ""

    def _opens(self, target, port: int) -> bool:
        refs = {*target.reached_by, *target.opened_by}
        return any(port in self.ssh_ports.get(ref, ()) for ref in refs)


# A fact on the tailnet connection: every port the policy opens on HQ's machine
# admits only named devices, users or tags, never anyone who joins.
HQ_ADMITS_ONLY_NAMED = "hq-admits-only-named"


def _policy_admits_only_named() -> bool:
    """Whether the tailnet policy admits only named principals to HQ's machine.

    False when it cannot say (no machine known to run HQ, no device for it, no
    policy read), so an unknown never silences the finding.
    """

    machine = next((item for item in connections.machines_once() if item.runs_hq), None)
    if machine is None or not machine.addresses:
        return False
    addresses = set(machine.addresses)
    device = next((item for item in devices().values() if addresses & set(item.addresses)), None)
    if device is None or not device.reach:
        return False
    return not any(
        principal == "*" or principal.startswith("autogroup:")
        for admitted in device.reach.values()
        for principal in admitted
    )


def posture_facts() -> tuple[tuple[str, str], ...]:
    """Policy settings worth a finding, as topology facts on the tailnet connection.

    Whether the policy admits only named principals to HQ's machine, and each
    weakness: devices joining unapproved, and a granted group or tag that no
    member or device holds.
    """

    found = policy()
    entries: list[tuple[str, str]] = []
    if _policy_admits_only_named():
        entries.append((HQ_ADMITS_ONLY_NAMED, "yes"))
    # Tailnet lock is approval by signature: every peer filters a new node out
    # until a signing key vouches for it, and the coordination server holds
    # none of those keys. Tailscale will not run device approval beside it, so
    # approval read as off under lock asks for something that cannot be on.
    if found.settings.get("devicesApprovalOn") is False and not found.lock.get("enabled"):
        entries.append(("devices-join-unapproved", "Off"))
    named = {
        name for rule in (*found.grants, *found.ssh_rules) for side in ("src", "dst") for name in rule.get(side) or ()
    }
    entries.extend(
        ("empty-group-granted", str(group.get("name", "")))
        for group in found.groups
        if not group.get("members") and group.get("name") in named
    )
    entries.extend(("tag-granted-to-nobody", tag) for tag in unworn_tags(named))
    return tuple(entries)


def unworn_tags(named) -> tuple[str, ...]:
    """Tags a rule names that no device carries, once devices were read.

    A rule's destination names a tag with its ports (``tag:web:443``); the tag
    is the part before them. Nothing is said while no device was read: every
    tag would look unworn.
    """

    records = [record for _snapshot, record in inventory_records(TAILNET_KIND)]
    if not records:
        return ()
    worn = {str(tag) for record in records for tag in record.get("tags") or ()}
    tags = {":".join(str(entry).split(":")[:2]) for entry in named if str(entry).startswith("tag:")}
    return tuple(sorted(tags - worn))
