"""Findings about a machine's edge: ports open to the internet, a perimeter nobody checked, a firewall that is not running, and reach nobody measured."""

from .finding_model import (
    THEN_CHECK_AGAIN,
    Finding,
    FindingEstate,
    FindingRule,
    Said,
    cannot_run_commands,
    connections_carrying,
    per_connection,
)
from .topology_model import TopologyNode
from .ui import ended


def _reached_but_unmeasured(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name a connection reports reaching that nothing is measuring.

    Infrastructure knows a connection reaches the name and analytics knows what
    each name it watches served; only the two together find the gap. An
    unmeasured site is a fact about HQ, where a measured one with no visitors is
    a fact about the site.
    """

    # Only observed targets (a live connection said it reaches them, so they
    # answer), and only beside a measured sibling: most of what HQ reaches never
    # carries a web beacon, but four measured names and a fifth without one is a
    # gap someone can close.
    measured_peers: dict[str, bool] = {}
    reached_by: dict[str, str] = {}
    for edge in estate.topology.edges:
        if edge.kind != "reaches":
            continue
        reached_by.setdefault(edge.target, edge.source)
    by_id = {node.id: node for node in estate.nodes()}
    for target_id, connection_id in reached_by.items():
        node = by_id.get(target_id)
        if node is not None and node.pageviews is not None:
            measured_peers[connection_id] = True

    return tuple(
        Finding(
            rule="reached-but-unmeasured",
            subject=node.id,
            title=f"{node.label} has no visitor counts",
            severity="attention",
            explanation=("Other names read through the same connection have them. Turn on analytics for this one."),
            evidence=(
                (
                    "Read through",
                    (by_id.get(reached_by[node.id]).label if by_id.get(reached_by[node.id]) else "a connection"),
                ),
                ("Visitor counts", "None"),
            ),
        )
        for node in estate.nodes()
        if node.kind in _REACHED_KINDS
        and node.pageviews is None
        and node.id in reached_by
        and measured_peers.get(reached_by[node.id])
    )


# Node kinds a connection reaches by name: a target, or the estate node it folded into.
_REACHED_KINDS = frozenset({"target", "service", "zone"})


def _perimeter_open(estate: FindingEstate) -> tuple[Finding, ...]:
    """A port answering from the public internet that nothing means to publish.

    Not a disagreement about configuration. The port was dialled from outside
    and it answered, so this is the invariant already broken rather than a
    prediction that it might be.
    """

    return per_connection(
        estate, "answers-publicly", rule="perimeter-open", severity="serious", shown_as="Open port", says=_open_ports
    )


def _open_ports(node: TopologyNode, ports: tuple[str, ...]) -> Said:
    one = len(ports) == 1
    return {
        "title": f"{node.label} is open to the internet on {'port' if one else 'ports'} {', '.join(ports)}",
        "explanation": f"A test from outside your network got an answer on {'this port' if one else 'these ports'}.",
    }


def _perimeter_unchecked(estate: FindingEstate) -> tuple[Finding, ...]:
    """A perimeter reading that tried nothing, which proves nothing either way."""

    found: list[Finding] = []
    for node, reasons in connections_carrying(estate, "perimeter-unchecked"):
        found.append(
            Finding(
                rule="perimeter-unchecked",
                subject=node.id,
                title=f"HQ could not test whether {node.label} is open to the internet",
                severity="attention",
                explanation=(
                    ended(
                        "The test did not run because "
                        + " and ".join(_UNCHECKED.get(reason, reason) for reason in reasons)
                    )
                ),
                evidence=tuple(("Not tested", _UNCHECKED.get(reason, reason)) for reason in reasons),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# Why the test tried nothing, as ``topology_facts.perimeter_unchecked`` words
# it, in a sentence.
_UNCHECKED = {
    "no public address": "the machine reported no public address",
    "no port to try": "the machine reported no port to try",
}


def _firewall_stopped(estate: FindingEstate) -> tuple[Finding, ...]:
    """A firewall that is installed, configured, and not running.

    Enabled and dead is the state with no symptom: the rules are on disk, the
    unit is listed, and nothing is filtering. It survives a reboot as silence,
    which is the one time it is most likely to happen.
    """

    found: list[Finding] = []
    for node, (state, *_) in connections_carrying(estate, "firewall-unit"):
        found.append(
            Finding(
                rule="firewall-stopped",
                subject=node.id,
                title=f"{node.label}'s firewall is not running",
                severity="serious",
                explanation="Its firewall rules are not being applied.",
                evidence=(("Firewall", _FIREWALL_STATES.get(state, state)),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# systemd's word for a unit that is not active, as a person says it.
_FIREWALL_STATES = {
    "inactive": "Stopped",
    "failed": "Failed",
    "activating": "Starting",
    "deactivating": "Stopping",
}

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "perimeter-open",
        "Ports open to the internet",
        "serious",
        _perimeter_open,
        operator_action=(f"Close them in the machine's firewall. {THEN_CHECK_AGAIN}"),
        no_help_reason=("HQ cannot change the firewall."),
    ),
    FindingRule(
        "perimeter-unchecked",
        "Could not test for ports open to the internet",
        "attention",
        _perimeter_unchecked,
        operator_action=(f"Make the machine report its public addresses and published ports. {THEN_CHECK_AGAIN}"),
        no_help_reason=("HQ cannot change what the machine reports."),
    ),
    FindingRule(
        "firewall-stopped",
        "A firewall is not running",
        "serious",
        _firewall_stopped,
        operator_action=("Start the firewall on the machine and check it stays running."),
        no_help_reason=cannot_run_commands(),
    ),
    FindingRule(
        "reached-but-unmeasured",
        "A name has no visitor counts",
        "attention",
        _reached_but_unmeasured,
        operator_action=("Turn on analytics for the name where the other names on its domain are measured."),
        no_help_reason=("HQ cannot turn analytics on."),
    ),
)
