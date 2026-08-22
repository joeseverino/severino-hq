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
            )
    return found


def ports() -> tuple[int, ...]:
    """Every port the sweep asked about, so the form offers exactly those."""

    return tuple(sorted({port for device in devices().values() for port in device.reach}))


def may_reach(source: str, target: str, port: int) -> Verdict:
    """Whether the policy admits ``source`` to ``target`` on ``port``.

    Three answers, not two. "Cannot say" is a real outcome and is kept distinct
    from "not allowed": a device nothing has swept, or a port nobody asked
    about, is a gap in what HQ was told rather than a decision the policy made.
    """

    known = devices()
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

    @property
    def known(self) -> bool:
        return bool(self.groups or self.tags or self.grants)


def policy() -> Policy:
    """What the tailnet's policy says, or an empty one if nothing swept it."""

    for snapshot in ProviderInventory.objects.filter(kind=POLICY_KIND):
        for record in snapshot.records:
            return Policy(
                groups=tuple(record.get("groups") or ()),
                tags=tuple(record.get("tags") or ()),
                grants=tuple(record.get("grants") or ()),
                tests=tuple(record.get("tests") or ()),
            )
    return Policy()
