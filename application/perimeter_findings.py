"""Findings about a machine's edge: ports open to the internet, a perimeter nobody checked, a firewall that is not running, and reach nobody measured."""

from __future__ import annotations

from .finding_model import (
    OperatorStep,
    Finding,
    FindingEstate,
    fact_values,
    FindingRule,
)


def _reached_but_unmeasured(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name a connection reports reaching that nothing is measuring.

    The first claim here that neither half of HQ can make alone. Infrastructure
    knows a connection reaches this name; analytics knows what every name it
    watches served. Put beside each other they answer a question neither was
    asked: which of the things we run is nobody watching.

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
            title=f"{node.label} has no traffic measurement",
            severity="attention",
            explanation=(
                "Other names on the same connection report traffic. This one "
                "does not. Add it to analytics."
            ),
            evidence=(
                (
                    "Reached by",
                    (
                        by_id.get(reached_by[node.id]).label
                        if by_id.get(reached_by[node.id])
                        else "a connection"
                    ),
                ),
                ("Traffic", "not measured"),
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
                    f"{node.label} answers on "
                    f"{', '.join(ports)} from the public internet"
                ),
                severity="serious",
                explanation=(
                    "A probe from outside the tailnet got an answer, so anything "
                    "behind these ports is public. Close them at the firewall."
                ),
                evidence=tuple(("Answers publicly", port) for port in ports),
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
                title=f"{node.label}'s public perimeter was not checked",
                steps=(
                    (
                        OperatorStep(
                            label="Make the perimeter command on this machine report "
                            "its public addresses, then request a fresh sweep."
                        ),
                    )
                    if "no public address" in reasons
                    else ()
                ),
                severity="attention",
                explanation=(
                    "The last reading dialled nothing, so it cannot say whether "
                    "anything answers from the public internet."
                ),
                evidence=tuple(("Not checked", reason) for reason in reasons),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


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
                title=f"{node.label} is not running its firewall",
                severity="serious",
                explanation=(
                    f"The firewall unit is {state}, so its rules are not "
                    "applied. Start the unit."
                ),
                evidence=(("Firewall unit", state),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "perimeter-open",
        "Port open to the public internet",
        "serious",
        _perimeter_open,
        operator_action=(
            "Close each port at the machine's firewall so it answers only on the tailnet, then request a fresh sweep."
        ),
        no_help_reason=(
            "HQ probes the machine from outside but has no write access to its firewall, and which ports close is yours to decide."
        ),
    ),
    FindingRule(
        "perimeter-unchecked",
        "Public perimeter not checked",
        "attention",
        _perimeter_unchecked,
        operator_action=(
            "Make the machine's perimeter reading report its public addresses and published ports, then request a fresh sweep."
        ),
        no_help_reason=(
            "The perimeter reading runs on the machine, and HQ cannot change what that command reports."
        ),
    ),
    FindingRule(
        "firewall-stopped",
        "Firewall not running",
        "serious",
        _firewall_stopped,
        operator_action=(
            "Start the firewall unit on the machine (systemctl start with the unit's name) and check it stays active."
        ),
        no_help_reason=(
            "The reading reports the unit's state but not its name, and HQ has no shell on the machine to start it."
        ),
    ),
    FindingRule(
        "reached-but-unmeasured",
        "Traffic not measured",
        "attention",
        _reached_but_unmeasured,
        operator_action=(
            "Add the hostname to the analytics source that measures its neighbours."
        ),
        no_help_reason=(
            "HQ reads the analytics source but no capability adds a name to it."
        ),
    ),
)
