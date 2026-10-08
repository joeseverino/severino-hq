"""Findings about a machine's edge: ports open to the internet, a perimeter nobody checked, a firewall that is not running, and reach nobody measured."""

from .finding_model import (
    THEN_CHECK_AGAIN,
    Finding,
    FindingEstate,
    FindingRule,
    cannot_run_commands,
    fact_values,
)


def _reached_but_unmeasured(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name a connection reports reaching that nothing is measuring.

    A claim neither half of HQ can make alone. Infrastructure knows a
    connection reaches this name; analytics knows what every name it watches
    served. Put beside each other they answer which of the things HQ reaches
    nobody is watching.

    ``None`` and zero are the whole rule. A measured site with no visitors is a
    fact about the site; an unmeasured one is a fact about HQ, and only the
    second is a gap someone can close.

    Restricted to observed targets on purpose. A declaration is a statement of
    intent and may name something not serving anything yet, but a target is a
    name a live connection said it *reaches*, so it is answering, and nothing
    is counting.

    Gated on a measured sibling, the same way a skipped record is judged against
    the sweep that confirmed its siblings. Most things HQ reaches are containers
    and proxy entries that will never carry a web beacon, and saying so about
    each would bury the queue in claims nobody can act on. But a connection with
    four measured names and a fifth without one is a gap someone can close, and
    that is the only shape this fires on.
    """

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

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        ports = fact_values(node, "answers-publicly")
        if not ports:
            continue
        found.append(
            Finding(
                rule="perimeter-open",
                subject=node.id,
                title=(
                    f"{node.label} is open to the internet on "
                    f"{'port' if len(ports) == 1 else 'ports'} {', '.join(ports)}"
                ),
                severity="serious",
                explanation=(
                    "A test from outside your network got an answer on "
                    f"{'this port' if len(ports) == 1 else 'these ports'}."
                ),
                evidence=tuple(("Open port", port) for port in ports),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _perimeter_unchecked(estate: FindingEstate) -> tuple[Finding, ...]:
    """A perimeter reading that tried nothing, which proves nothing either way."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        reasons = fact_values(node, "perimeter-unchecked")
        if not reasons:
            continue
        found.append(
            Finding(
                rule="perimeter-unchecked",
                subject=node.id,
                title=f"HQ could not test whether {node.label} is open to the internet",
                severity="attention",
                explanation=(
                    "The test did not run because "
                    + " and ".join(_UNCHECKED.get(reason, reason) for reason in reasons)
                    + "."
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
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        state = next(
            (value for key, value in node.facts if key == "firewall-unit" and value),
            "",
        )
        if not state:
            continue
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
