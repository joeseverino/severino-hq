"""Findings about reach: a connection that does not answer, and a certificate consumer HQ cannot get to."""

from hq.domains.control_plane.provider_adapters.contracts import ADDRESS_FAILURE

from . import credential_findings
from .connection_lifecycle import STATE_FACT, Lifecycle
from .credential_findings import mint_steps
from .entity_links import entity_link
from .finding_model import (
    Finding,
    FindingEstate,
    FindingRule,
    fact_values,
    open_the_path,
    reconcile_remedy,
)
from .moments import when
from .tone import Tone
from .ui import agreed, ended
lazy from .topology_facts import PATH_DENIED, UNREACHABLE


def _connection_not_answering(estate: FindingEstate) -> tuple[Finding, ...]:
    """A connection whose last probe got no answer, or whose credential is refused."""

    found: list[Finding] = []
    for node in estate.nodes():
        # Said once for its controller, by connections-lost.
        if node.kind != "connection" or fact_values(node, credential_findings.UNREPORTED):
            continue
        if not set(fact_values(node, STATE_FACT)) & {Lifecycle.UNREACHABLE, Lifecycle.REVOKED}:
            continue
        refusal = next((value for key, value in node.facts if key == "credential-refused"), None)
        refused = refusal is not None
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
                severity=Tone.ATTENTION,
                explanation=ended(reason) if reason else "HQ cannot read through it.",
                evidence=(
                    ("State", node.status_label),
                    *((("Cause", credential_findings.FAILURE_LABELS[failure]),) if failure and not refused else ()),
                    *((("Last checked", when(node.observed_at)),) if node.observed_at else ()),
                ),
                fixes=mint_steps(node) if refused else credential_findings.answer_steps(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _service(node) -> str:
    """What a connection talks to, by the name its owner knows it by."""

    return node.subtitle or "the service"


def _unreachable_consumer(estate: FindingEstate) -> tuple[Finding, ...]:
    """A name this estate serves that the last reading could not reach.

    A claim about one path, from the machine that verifies to the machine that
    serves, never that the certificate is wrong: the other consumers were read
    and agree. Left unread, a consumer keeps whatever it was last given and
    nothing would notice it going stale, which this rule exists to make loud.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        # Every entry shares one key, so a dictionary of the node's facts would
        # report one unreachable name however many there were.
        names = fact_values(node, UNREACHABLE)
        if not names:
            continue
        # The tailnet refusing the path is a different claim from the consumer
        # being down, and only one of them has a fix worth offering. Present,
        # the policy was asked and said no; absent, it said yes or was never
        # swept, and neither is a reason to send anybody at an access policy.
        refused = fact_values(node, PATH_DENIED)
        found.append(
            Finding(
                rule="unreachable-consumer",
                subject=node.id,
                title=(
                    f"Could not check {node.label} on {names[0]}"
                    if len(names) == 1
                    else f"Could not check {node.label} in {len(names)} places"
                ),
                severity=Tone.SERIOUS,
                explanation=agreed(
                    len(names),
                    *(
                        ("The tailnet policy blocks it.", "The tailnet policy blocks them.")
                        if refused
                        else ("It did not answer.", "They did not answer.")
                    ),
                ),
                evidence=(
                    *(("Did not answer", entity_link("service", name)) for name in names),
                    *(("Blocked by tailnet policy", path) for path in refused),
                ),
                fixes=open_the_path(node) if refused else reconcile_remedy(node),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "connection-not-answering",
        "Connection not answering",
        Tone.ATTENTION,
        _connection_not_answering,
        operator_action="Open the connection and fix what its error says.",
        no_help_reason="HQ cannot change 1Password or your network.",
        bars={Tone.ATTENTION: "a connection fails its check"},
    ),
    FindingRule(
        "unreachable-consumer",
        "Certificate not checked",
        Tone.SERIOUS,
        _unreachable_consumer,
        operator_action="Make it reachable from the controller's machine.",
        no_help_reason="HQ cannot reach it any other way.",
        # Says the same thing with the name of the consumer in it. The generic
        # rule would otherwise put this in front of an operator twice.
        subsumes=("reporting-a-fault",),
        bars={Tone.SERIOUS: "a place a certificate is installed does not answer"},
    ),
)
