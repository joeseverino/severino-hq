"""What the finding rules that read HQ's own records find (``FindingRule.gathered``).

They run as the estate is gathered, before derivation, so deriving reads
nothing of its own. A rule about a section's records reads them only for a
reader of what the section declares.
"""

from .finding_model import FindingEstate, FindingRule, Gathered
from .integration_validation import required_capability_names
from .integrations import held_graph
from .security import Principal
lazy from .domains import resources_of
lazy from .findings import finding_rules


def gather_findings(estate: FindingEstate, *, principal: Principal, rule: str = "") -> Gathered:
    """What each rule that reads HQ's records finds, by rule; only ``rule``'s when one is asked."""

    return {
        declared.name: declared.detect(estate)
        for declared in finding_rules()
        if declared.gathered and (not rule or declared.name == rule) and _reads_section(principal, declared)
    }


def _reads_section(principal: Principal, rule: FindingRule) -> bool:
    resources = held_graph().resources
    return principal.permits(
        *(gate for name in resources_of(rule.domain) for gate in required_capability_names(resources[name]))
    )
