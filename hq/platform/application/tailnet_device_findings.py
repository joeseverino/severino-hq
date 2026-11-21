"""Findings about each machine's own standing on the tailnet.

Facts with no symptom until somebody needs them: a node key that runs out on a
date set months earlier, a route advertised but never approved.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any

from hq.domains.control_plane.consoles import tailscale_machine

from .expiry import days_until
from .finding_model import TAILNET_CARD, Finding, FindingEstate, FindingRemedy, FindingRule
from .finding_steps import Do, Run
from .images import compare, version
from .moments import moment, when_day
from .projection import read_once
from .reading_facts import by_node, stands
from .thresholds import Threshold
from .tone import Tone
from .topology_model import TopologyNode
from .ui import counted, ended
lazy from .tailnet import policy
lazy from .tailnet_presence import Presence, tailnet_presence
lazy from .tailnet_signers import signers

# A node key lasts 180 days here, so a warning at 75 would sit in the queue for
# more of the cycle than not, and a queue that is always non-empty is one nobody
# reads. Forty-five days is several unhurried weekends; fourteen is the point at
# which it stops being a plan and starts being a date.
KEY_EXPIRY_ATTENTION_DAYS = Threshold(45, "days")
KEY_EXPIRY_SERIOUS_DAYS = Threshold(14, "days")


@dataclass(frozen=True, slots=True)
class TailnetDevice:
    """What the tailnet says of one device that a rule here reads.

    Judged against now, never as of when it was read: a key's expiry is a
    date, and the rest hold until the next reading says otherwise.
    """

    key_expires: str = ""
    unauthorized: bool = False
    lock_error: str = ""
    update: bool = False
    # The release it runs, and the newest any device reports, each numeric.
    running: str = ""
    newest: str = ""
    # Routes it offers that the tailnet has not approved.
    unapproved: tuple[str, ...] = ()
    # It offers itself as an exit node and that is not approved. The swept
    # fact, never a second reading of the route list: what makes a route an
    # exit route is Tailscale's to say, and the sweep already asked.
    exit_unapproved: bool = False
    # The device's page in the Tailscale admin console, where its key and approval are changed.
    console: str = ""


def _release(reported: str) -> str:
    """The numeric release a client reports, "1.102.3-t0abc" as "1.102.3"."""

    return ".".join(str(part) for part in version(reported))


# Releases by number, so 1.80 and 1.80.0 are one release.
_BY_RELEASE = cmp_to_key(lambda one, other: compare(version(one), version(other)))


def _row(presence: Presence, newest: str) -> TailnetDevice:
    return TailnetDevice(
        key_expires=presence.key_expires,
        unauthorized=not presence.authorized,
        lock_error=presence.lock_error,
        update=presence.update_available,
        running=_release(presence.client_version) or presence.client_version,
        newest=newest,
        unapproved=presence.unapproved_routes,
        exit_unapproved=presence.offers_exit_node and not presence.exit_node_approved,
        console=tailscale_machine({"addresses": presence.addresses}),
    )


def _device_rows() -> tuple[tuple[str, TailnetDevice], ...]:
    """Each device's row, by the name of the machine it belongs to, read once per projection."""

    return read_once("tailnet_device_findings.rows", _read_rows)


def _read_rows() -> tuple[tuple[str, TailnetDevice], ...]:
    present = tailnet_presence()
    reported = [presence.client_version for presence in present.values() if version(presence.client_version)]
    newest = _release(max(reported, key=_BY_RELEASE, default=""))
    return tuple((name, _row(presence, newest)) for name, presence in present.items())


def _signing_servers() -> tuple[tuple[str, str], ...]:
    """Each server tailnet lock trusts to sign, by its machine's name, with its key."""

    return tuple((signer.machine, signer.key) for signer in signers() if signer.server)


def add(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> None:
    """The node of each machine a device, a device tailnet lock keeps out,
    or a signing server belongs to."""

    # A device tailnet lock filters out is a fact about the tailnet rather than
    # about the device's presence: a locked-out node is left out of the device
    # reading, so asking each device would never find one.
    names = (
        *(name for name, _row in _device_rows()),
        *policy().locked_out,
        *(name for name, _key in _signing_servers()),
    )
    for name in names:
        stands(nodes, machine, "", name)


def _devices(estate: FindingEstate) -> tuple[tuple[TopologyNode, list[TailnetDevice]], ...]:
    return by_node(estate, (("", name, row) for name, row in _device_rows()))


def _each(estate: FindingEstate, says: Callable[[TopologyNode, TailnetDevice], Finding | None]) -> tuple[Finding, ...]:
    """One finding per machine, from the first of its devices ``says`` raises one for."""

    found = []
    for node, rows in _devices(estate):
        finding = next((said for row in rows if (said := says(node, row)) is not None), None)
        if finding is not None:
            found.append(finding)
    return tuple(found)


def _key_expiring(estate: FindingEstate) -> tuple[Finding, ...]:
    """A node key that runs out within the window, or has run out."""

    def says(node: TopologyNode, row: TailnetDevice) -> Finding | None:
        expires = moment(row.key_expires)
        # A device with key expiry disabled has no date, so it is silent here.
        if expires is None or (days := days_until(expires, estate.now)) > KEY_EXPIRY_ATTENTION_DAYS.value:
            return None
        name = node.label
        return Finding(
            rule="tailnet-key-expiring",
            subject=node.id,
            severity=Tone.SERIOUS if days <= KEY_EXPIRY_SERIOUS_DAYS.value else Tone.ATTENTION,
            title=f"{name} leaves the tailnet in {counted(days, 'day')}"
            if days > 0
            else f"{name} has left the tailnet",
            explanation=ended(f"Its Tailscale key {'expires' if days > 0 else 'expired'} on {when_day(expires)}"),
            steps=(
                Do(
                    (
                        f"Sign in to Tailscale again on {name}, or turn off key expiry for it in the Tailscale admin "
                        "console."
                        if days > 0
                        else ended(f"Sign in to Tailscale again on {name}")
                    ),
                    notes=_console(row),
                ),
            ),
        )

    return _each(estate, says)


def _console(row: TailnetDevice) -> tuple[str, ...]:
    """Where in the Tailscale admin console the step is done, when the device has an address to find it by."""

    return (row.console,) if row.console else ()


def _sign_key(name: str) -> tuple[Run, ...]:
    """Signing takes a tailnet lock key, which only a signing machine holds."""

    return (
        Run(
            label=f"On a machine that can sign, list the keys waiting, then run tailscale lock sign with the key it "
            f"lists for {name}",
            command="tailscale lock status",
        ),
    )


def _locked_out(estate: FindingEstate) -> tuple[Finding, ...]:
    """A device tailnet lock filters out, whose own status still reads healthy."""

    return tuple(
        Finding(
            rule="tailnet-device-locked-out",
            subject=node.id,
            severity=Tone.SERIOUS,
            title=f"{node.label} is locked out of the tailnet",
            explanation=(
                f"Other machines ignore {node.label} because its key is not signed. Its own status still shows healthy."
            ),
            steps=_sign_key(node.label),
        )
        for node, _names in by_node(estate, (("", name, name) for name in policy().locked_out))
    )


def _unauthorized(estate: FindingEstate) -> tuple[Finding, ...]:
    """A device the tailnet has not authorized, which reaches nothing."""

    return _each(
        estate,
        lambda node, row: (
            Finding(
                rule="tailnet-device-unapproved",
                subject=node.id,
                severity=Tone.SERIOUS,
                title=f"{node.label} is waiting for tailnet approval",
                explanation="It cannot reach anything yet.",
                steps=(Do("Approve it in the Tailscale admin console, under Machines.", notes=_console(row)),),
            )
            if row.unauthorized
            else None
        ),
    )


def _lock_unsigned(estate: FindingEstate) -> tuple[Finding, ...]:
    """A device carrying a lock error, which nothing under tailnet lock reaches."""

    return _each(
        estate,
        lambda node, row: (
            Finding(
                rule="tailnet-lock-unsigned",
                subject=node.id,
                severity=Tone.SERIOUS,
                title=f"{node.label} is not signed for tailnet lock",
                explanation=(
                    f"Other machines ignore {node.label} because its key is not signed. "
                    f"Tailscale says: {ended(row.lock_error)}"
                ),
                steps=_sign_key(node.label),
            )
            if row.lock_error
            else None
        ),
    )


def _server_signs(estate: FindingEstate) -> tuple[Finding, ...]:
    """A server whose key tailnet lock trusts to sign, so whoever takes it
    over can add devices to the tailnet."""

    return tuple(
        Finding(
            rule="tailnet-lock-server-signs",
            subject=node.id,
            severity=Tone.SERIOUS,
            title=f"{node.label} can sign devices onto the tailnet",
            explanation=f"Whoever takes over {node.label} can add devices to the tailnet.",
            steps=tuple(
                Run(
                    label="On another signing device, remove its key",
                    command=f"tailscale lock remove {key}",
                )
                for key in keys
            ),
        )
        for node, keys in by_node(estate, (("", name, key) for name, key in _signing_servers()))
    )


def _update_said(row: TailnetDevice) -> str:
    if not row.running:
        return "Current version not reported."
    if row.newest and compare(version(row.newest), version(row.running)) > 0:
        return ended(f"Runs {row.running}; {row.newest} is out")
    return ended(f"Runs {row.running}")


def _client_update(estate: FindingEstate) -> tuple[Finding, ...]:
    """A client left behind, which is how a fleet acquires versions nobody chose."""

    return _each(
        estate,
        lambda node, row: (
            Finding(
                rule="tailnet-client-update",
                subject=node.id,
                severity=Tone.ATTENTION,
                title=f"Tailscale update available for {node.label}",
                explanation=_update_said(row),
                steps=(Run(f"On {node.label}, as an administrator", "tailscale update"),),
            )
            if row.update
            else None
        ),
    )


def _routes_unapproved(estate: FindingEstate) -> tuple[Finding, ...]:
    """Routes a machine offers that the tailnet hands to nobody."""

    def says(node: TopologyNode, row: TailnetDevice) -> Finding | None:
        unapproved = row.unapproved
        if not unapproved:
            return None
        exit_node = (
            "It cannot be used as an exit node until its exit route is approved. " if row.exit_unapproved else ""
        )
        return Finding(
            rule="tailnet-routes-unapproved",
            subject=node.id,
            severity=Tone.ATTENTION,
            title=f"{node.label} offers "
            + counted(len(unapproved), "route that is not approved yet", "routes that are not approved yet"),
            explanation=(
                f"Not approved: {ended(', '.join(unapproved))} {exit_node}"
                "Approve the ones you want and stop offering the rest."
            ),
            # The approval the machine's own page offers.
            remedies=(FindingRemedy(capability="tailnet.routes.approve", target=node.label, url=node.url),)
            if node.url
            else (),
        )

    return _each(estate, says)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "tailnet-key-expiring",
        "A tailnet key runs out soon",
        Tone.ATTENTION,
        _key_expiring,
        gathered=True,
        operator_action=(
            "Sign in to Tailscale again on the machine, or turn off key expiry for it in the Tailscale admin console."
        ),
        no_help_reason="HQ cannot sign a machine in to the tailnet.",
        card=TAILNET_CARD,
        bars={
            Tone.SERIOUS: f"a machine's tailnet key runs out within {KEY_EXPIRY_SERIOUS_DAYS}",
            Tone.ATTENTION: f"a machine's tailnet key runs out within {KEY_EXPIRY_ATTENTION_DAYS}",
        },
        clears=f"the key runs more than {KEY_EXPIRY_ATTENTION_DAYS} out, or key expiry is off",
    ),
    FindingRule(
        "tailnet-device-locked-out",
        "A device locked out by tailnet lock",
        Tone.SERIOUS,
        _locked_out,
        gathered=True,
        operator_action="On a machine that can sign, run tailscale lock sign with the machine's key.",
        no_help_reason="Only a machine holding a tailnet lock key can sign.",
        card=TAILNET_CARD,
        bars={Tone.SERIOUS: "tailnet lock keeps a device off the tailnet"},
    ),
    FindingRule(
        "tailnet-device-unapproved",
        "A device waiting for tailnet approval",
        Tone.SERIOUS,
        _unauthorized,
        gathered=True,
        operator_action="Approve it in the Tailscale admin console, under Machines.",
        no_help_reason="HQ cannot approve a device on the tailnet.",
        card=TAILNET_CARD,
        bars={Tone.SERIOUS: "a device waits for approval to join the tailnet"},
    ),
    FindingRule(
        "tailnet-lock-unsigned",
        "A device not signed for tailnet lock",
        Tone.SERIOUS,
        _lock_unsigned,
        gathered=True,
        operator_action="On a machine that can sign, run tailscale lock sign with the machine's key.",
        no_help_reason="Only a machine holding a tailnet lock key can sign.",
        card=TAILNET_CARD,
        bars={Tone.SERIOUS: "a device has no tailnet lock signature"},
    ),
    FindingRule(
        "tailnet-lock-server-signs",
        "A server can sign for tailnet lock",
        Tone.SERIOUS,
        _server_signs,
        gathered=True,
        operator_action="On a signing device other than the server, run tailscale lock remove with the server's key.",
        no_help_reason="Only a device that can sign changes the keys tailnet lock trusts.",
        card=TAILNET_CARD,
        bars={Tone.SERIOUS: "tailnet lock trusts a server's key to sign"},
    ),
    FindingRule(
        "tailnet-client-update",
        "A tailnet client update is available",
        Tone.ATTENTION,
        _client_update,
        gathered=True,
        operator_action="Run tailscale update on the machine, as an administrator.",
        no_help_reason="HQ cannot run commands on the machine.",
        card=TAILNET_CARD,
        bars={Tone.ATTENTION: "a newer Tailscale client is out for a device"},
    ),
    FindingRule(
        "tailnet-routes-unapproved",
        "Routes offered but not approved",
        Tone.ATTENTION,
        _routes_unapproved,
        gathered=True,
        operator_action="Approve the routes you want on the machine's page, and stop offering the rest.",
        no_help_reason="HQ approves only the routes a machine already offers, and you decide which.",
        card=TAILNET_CARD,
        bars={Tone.ATTENTION: "a machine offers routes nobody has approved"},
    ),
)
