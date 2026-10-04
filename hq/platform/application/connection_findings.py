"""Findings about reach: a connection that does not answer, and a certificate consumer HQ cannot get to."""

from __future__ import annotations

from hq.domains.control_plane.provider_adapters.contracts import ADDRESS_FAILURE

from . import credential_findings
from .credential_findings import mint_steps
from .finding_model import (
    Finding,
    FindingEstate,
    fact_values,
    open_the_path,
    reconcile_remedy,
    FindingRule,
)


def _connection_not_answering(estate: FindingEstate) -> tuple[Finding, ...]:
    """A connection whose last probe got no answer, or whose credential is refused."""

    found: list[Finding] = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        refusal = next(
            (value for key, value in node.facts if key == "credential-refused"), None
        )
        refused = refusal is not None
        if node.status != "serious" and not refused:
            continue
        reason = refusal or node.detail
        failure = credential_findings.failure_of_node(node)
        found.append(
            Finding(
                rule="connection-not-answering",
                subject=node.id,
                title=(
                    f"{node.label}'s credential is refused"
                    if refused
                    else f"{node.label} does not answer as its API"
                    if failure == ADDRESS_FAILURE
                    else f"{node.label} is not answering"
                ),
                severity="attention",
                explanation=(
                    (f"{reason.rstrip('.')}. " if reason else "")
                    + (
                        "The provider refuses the credential itself, so HQ reads "
                        "nothing through it until it is replaced."
                        if refused
                        else "HQ reads nothing through it until it answers, so what "
                        "it reaches may be out of date."
                    )
                ),
                evidence=(
                    ("State", "Refused" if refused else node.status_label or "Unreachable"),
                    *(
                        (("Cause", credential_findings.FAILURE_LABELS[failure]),)
                        if failure and not refused
                        else ()
                    ),
                    *((("Last observed", node.observed_at),) if node.observed_at else ()),
                ),
                steps=mint_steps(node) if refused else credential_findings.answer_steps(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _unreachable_consumer(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name this estate serves that the last reading could not reach.

    Not a claim that the certificate is wrong: the other consumers were read
    and agree. It is a claim about one path, from the machine that verifies to
    the machine that serves, and a path is the kind of thing a network policy
    decides rather than a certificate. Left unread, a consumer keeps whatever
    it was last given and nothing here would ever notice it going stale, which
    is the failure this rule exists to make loud.

    Read from the node's facts rather than a dictionary of them: every entry
    here shares one key, and collapsing them would report one unreachable name
    however many there were.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        names = fact_values(node, "unreachable")
        if not names:
            continue
        # The tailnet refusing the path is a different claim from the consumer
        # being down, and only one of them has a fix worth offering. Present,
        # the policy was asked and said no; absent, it said yes or was never
        # swept, and neither is a reason to send anybody at an access policy.
        refused = fact_values(node, "path-denied")
        found.append(
            Finding(
                rule="unreachable-consumer",
                subject=node.id,
                title=(
                    f"{node.label} could not read {names[0]}"
                    if len(names) == 1
                    else f"{node.label} could not read {len(names)} of its consumers"
                ),
                severity="serious",
                explanation=(
                    "The last reading reached every other consumer. This one "
                    "did not answer, so HQ cannot tell which certificate it "
                    "serves."
                    + (
                        " The tailnet policy blocks the path. Allow it, then "
                        "reconcile."
                        if refused
                        else ""
                    )
                ),
                evidence=(
                    *(("Not read", name) for name in names),
                    *(("Blocked by tailnet policy", path) for path in refused),
                ),
                remedies=open_the_path(node) if refused else reconcile_remedy(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "connection-not-answering",
        "Connection not answering or refused",
        "attention",
        _connection_not_answering,
        operator_action=(
            "Fix what the connection's error names (its address, its credential, or the route to it) in its 1Password item or on the network, then request a fresh sweep."
        ),
        no_help_reason=(
            "The fault is outside HQ: the address, the credential in 1Password, or the network path, none of which HQ writes."
        ),
    ),
    FindingRule(
        "unreachable-consumer",
        "Consumer could not be read",
        "serious",
        _unreachable_consumer,
        operator_action=(
            "Make the consumer reachable from the controller, or allow the path in the tailnet policy, then reconcile."
        ),
        no_help_reason=(
            "When the tailnet does not refuse the path and the kind is locked against reconcile, HQ has nothing left it may try."
        ),
        # Says the same thing with the name of the consumer in it. The generic
        # rule would otherwise put this in front of an operator twice.
        subsumes=("reporting-a-fault",),
    ),
)
