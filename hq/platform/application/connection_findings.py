"""Findings about reach: a connection that does not answer, and a certificate consumer HQ cannot get to."""

from hq.domains.control_plane.provider_adapters.contracts import ADDRESS_FAILURE

from . import credential_findings
from .credential_findings import mint_steps
from .finding_model import (
    THEN_CHECK_AGAIN,
    Finding,
    FindingEstate,
    FindingRule,
    fact_values,
    open_the_path,
    reconcile_remedy,
)
from .moments import elapsed


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
                    f"{node.label}: credential refused"
                    if refused
                    else f"{node.label}: the address is not {_service(node)}'s API"
                    if failure == ADDRESS_FAILURE
                    else f"{node.label} is not answering"
                ),
                severity="attention",
                explanation=(
                    (f"{reason.rstrip('.')}. " if reason else "")
                    + "HQ cannot read anything through it, so what it shows from "
                    f"{node.label} may be out of date."
                ),
                evidence=(
                    ("State", "Refused" if refused else node.status_label or "Unreachable"),
                    *(
                        (("Cause", credential_findings.FAILURE_LABELS[failure]),)
                        if failure and not refused
                        else ()
                    ),
                    *((("Last checked", elapsed(node.observed_at)),) if node.observed_at else ()),
                ),
                steps=mint_steps(node) if refused else credential_findings.answer_steps(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _service(node) -> str:
    """What a connection talks to, by the name its owner knows it by."""

    return node.subtitle or "the service"


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
                    f"Could not check {node.label} on {names[0]}"
                    if len(names) == 1
                    else f"Could not check {node.label} on {len(names)} of the "
                    "places it is installed"
                ),
                severity="serious",
                explanation=(
                    "The other places answered. "
                    + (
                        f"{names[0]} did not, so HQ cannot say which certificate it is serving."
                        if len(names) == 1
                        else "These did not, so HQ cannot say which certificate they are serving."
                    )
                    + (
                        f" The tailnet policy does not allow {', '.join(refused)}."
                        if refused
                        else ""
                    )
                ),
                evidence=(
                    *(("Did not answer", name) for name in names),
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
        "A connection is not answering",
        "attention",
        _connection_not_answering,
        operator_action=(
            f"Open the connection and fix what its error says. {THEN_CHECK_AGAIN}"
        ),
        no_help_reason=(
            "HQ cannot change 1Password or your network."
        ),
    ),
    FindingRule(
        "unreachable-consumer",
        "Could not check a certificate where it is installed",
        "serious",
        _unreachable_consumer,
        operator_action=(
            f"Make it reachable from the controller's machine. {THEN_CHECK_AGAIN}"
        ),
        no_help_reason=(
            "HQ cannot reach it any other way."
        ),
        # Says the same thing with the name of the consumer in it. The generic
        # rule would otherwise put this in front of an operator twice.
        subsumes=("reporting-a-fault",),
    ),
)
