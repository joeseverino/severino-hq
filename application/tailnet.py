"""Whether one machine on the tailnet may reach another, and on which port.

HQ does not evaluate the policy. Tailscale does, during the sweep: for each
device and port it is asked which principals a rule admits, and the answer --
with groups already flattened to the users in them -- is what gets stored. What
happens here is set membership. That distinction is the whole design: a second
implementation of an access policy would be believed exactly as much as the
real one and wrong in ways nobody notices until it matters.

So every verdict below is reported as what the policy says, never as what
Tailscale did, and a question the sweep cannot answer says so rather than
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from control_plane.models import ProviderInventory

TAILNET_KIND = "tailscale.device"


@dataclass(frozen=True)
class Device:
    """One machine as the policy names it: an owner, some tags, some ports."""

    name: str
    user: str = ""
    tags: tuple[str, ...] = ()
    reach: dict[int, tuple[str, ...]] = field(default_factory=dict)
    rules: dict[int, tuple[dict, ...]] = field(default_factory=dict)
    addresses: tuple[str, ...] = ()
    dns_name: str = ""
    os: str = ""
    online: bool = False
    observer: bool = False
    # How the observer is talking to it, as that node's own daemon reports it.
    direct_endpoint: str = ""
    relay: str = ""
    last_handshake: str = ""
    active: bool = False
    rx_bytes: int = 0
    tx_bytes: int = 0
    endpoints: tuple[str, ...] = ()
    key_expires: str = ""
    exit_node: bool = False
    observed_at: datetime | None = None

    @property
    def label(self) -> str:
        """The name worth showing a person for this device.

        A node registers under whatever its operating system calls itself, and
        several of them call themselves the same unhelpful thing -- a phone
        reporting "localhost" is not a bug in the sweep, it is the hostname.
        The MagicDNS label is the tailnet's own name for the node and is unique
        within it by construction, so it wins wherever the two disagree.
        """

        magic = self.dns_name.partition(".")[0]
        return magic or self.name

    @property
    def path(self) -> str:
        """Direct, relayed, or not currently negotiated -- in those words.

        A relayed peer still works; it is slower and it crosses a machine
        neither end owns. Saying which is the point: the two look identical
        from every other surface in HQ.
        """

        if self.direct_endpoint:
            return "direct"
        if self.relay:
            return "relayed"
        return "idle"

    @property
    def principals(self) -> frozenset[str]:
        """Every name a rule could admit this device by."""

        return frozenset({self.user, *self.tags} - {""})

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(sorted(self.reach))

    @property
    def openings(self) -> tuple[tuple[int, tuple[str, ...]], ...]:
        """``(port, who)`` in port order, for a template that cannot index."""

        return tuple((port, self.reach[port]) for port in self.ports)


@dataclass(frozen=True)
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

    found: dict[str, Device] = {}
    for snapshot in ProviderInventory.objects.filter(kind=TAILNET_KIND):
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
                addresses=tuple(
                    str(address) for address in record.get("addresses") or ()
                ),
                dns_name=str(record.get("dns_name", "")),
                os=str(record.get("os", "")),
                online=bool(record.get("online")),
                observer=bool(record.get("self")),
                direct_endpoint=str(record.get("direct_endpoint", "")),
                relay=str(record.get("relay", "")),
                last_handshake=str(record.get("last_handshake", "")),
                active=bool(record.get("active")),
                rx_bytes=int(record.get("rx_bytes") or 0),
                tx_bytes=int(record.get("tx_bytes") or 0),
                endpoints=tuple(
                    str(endpoint) for endpoint in record.get("endpoints") or ()
                ),
                key_expires=str(record.get("key_expires", "")),
                exit_node=bool(record.get("exit_node")),
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
        (
            found
            for found in (known if known is not None else devices()).values()
            if wanted in found.addresses
        ),
        None,
    )


def observer(known: dict[str, Device] | None = None) -> Device | None:
    """The device whose daemon took the reading -- the one HQ runs on."""

    return next(
        (
            found
            for found in (known if known is not None else devices()).values()
            if found.observer
        ),
        None,
    )


def ports() -> tuple[int, ...]:
    """Every port the sweep asked about, so the form offers exactly those."""

    return tuple(sorted({port for device in devices().values() for port in device.reach}))


def may_reach(
    source: str, target: str, port: int, known: dict[str, Device] | None = None
) -> Verdict:
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
        return Verdict(
            False, False, f"{missing} is not a device the last sweep described."
        )
    if not who_asks.principals:
        return Verdict(
            False,
            False,
            f"{source} carries no user or tag, so no rule can name it. It may "
            "not have been seen by a credential that reports identity.",
        )
    admitted = who_answers.reach.get(port)
    if admitted is None:
        return Verdict(
            False,
            False,
            f"Nothing was asked about port {port} on {target}, so HQ has no "
            "answer for it either way.",
        )
    matched = tuple(sorted(who_asks.principals & set(admitted)))
    if matched:
        return Verdict(
            True,
            True,
            f"The policy admits {', '.join(matched)} to {target} on {port}.",
            via=matched,
            rules=who_answers.rules.get(port, ()),
        )
    return Verdict(
        False,
        True,
        f"No rule admits {', '.join(sorted(who_asks.principals))} to {target} "
        f"on {port}. It is open to {', '.join(admitted)}.",
        rules=who_answers.rules.get(port, ()),
    )


POLICY_KIND = "tailscale.policy"


@dataclass(frozen=True)
class Policy:
    """The policy as HQ last read it, in the shape a page renders."""

    groups: tuple[dict, ...] = ()
    tags: tuple[dict, ...] = ()
    grants: tuple[dict, ...] = ()
    tests: tuple[dict, ...] = ()
    settings: dict = field(default_factory=dict)
    dns: dict = field(default_factory=dict)

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
            rows.append(("Resolves through", ", ".join(nameservers)))
        rows.append(("MagicDNS", "On" if self.dns.get("magicDNS") else "Off"))
        days = self.settings.get("devicesKeyDurationDays")
        if days:
            rows.append(("Node keys last", f"{days} days"))
        rows.append((
            "New devices",
            "Need approval" if self.settings.get("devicesApprovalOn") else "Join without approval",
        ))
        rows.append((
            "New users",
            "Need approval" if self.settings.get("usersApprovalOn") else "Join without approval",
        ))
        rows.append((
            "Client updates",
            "Automatic" if self.settings.get("devicesAutoUpdatesOn") else "Manual",
        ))
        rows.append((
            "Policy authored",
            "Outside Tailscale" if self.settings.get("aclsExternallyManagedOn") else "In Tailscale",
        ))
        return tuple(rows)

    @property
    def known(self) -> bool:
        return bool(self.groups or self.tags or self.grants)


def declaration() -> str:
    """The key of the policy declaration, when one has been adopted.

    What turns every rule on the page from something to read into something to
    change: with it, each row can point at the form that edits the document it
    came from.
    """

    from control_plane.models import ManagedResource

    return (
        ManagedResource.objects.filter(kind=POLICY_KIND, enabled=True)
        .values_list("key", flat=True)
        .first()
        or ""
    )


def proposed_grant(source: str, target: str, port: int) -> dict:
    """The grant that would allow a thing the policy currently refuses.

    Offered rather than applied. It is written in the terms the policy already
    uses -- a tag where the machine carries one, the owner where it does not --
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


def policy() -> Policy:
    """What the tailnet's policy says, or an empty one if nothing swept it."""

    for snapshot in ProviderInventory.objects.filter(kind=POLICY_KIND):
        for record in snapshot.records:
            return Policy(
                groups=tuple(record.get("groups") or ()),
                tags=tuple(record.get("tags") or ()),
                grants=tuple(record.get("grants") or ()),
                tests=tuple(record.get("tests") or ()),
                settings=record.get("settings") or {},
                dns=record.get("dns") or {},
            )
    return Policy()
