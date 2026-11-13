"""The default hold: which changes wait for a person before any operator rule.

One rule under the capability runner's hold and the use cases' write floor
(``consent_gap``), so the two cannot disagree, and read by the provider
contract and the policy page to say it.
"""

from typing import Any

from hq.domains.control_plane.models import ManagedResource
lazy from hq.domains.control_plane.providers import PROVIDERS

from .approvals import DECLARATIONS, READ_EFFECT
from .record_errors import Sentence
from .security import Principal, is_agent, is_interactive
lazy from .adoption import ADOPT, ADOPT_SERVICE
lazy from .zones import ADOPT_ZONE


def may_be_held_by_default(spec) -> bool:
    """The capability-level half of held_by_default, for describing a default without a target."""

    return spec.effect != READ_EFFECT and spec.subject_resource == DECLARATIONS


def held_by_default(spec, command: Any, target: Any, *, principal: Principal) -> bool:
    """The default before any operator rule: a change to a gated declaration waits."""

    if not may_be_held_by_default(spec):
        return False
    if target is not None:
        kind = ManagedResource.objects.filter(key=str(target)).values_list("kind", flat=True).first()
    else:
        kind = str(getattr(command, "kind", ""))
    provider = PROVIDERS.get(kind) if kind else None
    return provider is not None and _waits(provider, principal, spec)


def needs_approval(provider, spec=None) -> bool:
    """Whether an agent's change to this kind waits for a person, asked by ``spec`` when it is known.

    The unit is the resource kind, never the capability effect: an effect says
    how forceful an act is, not how much stands behind what it acts on, and
    gating it would put every container restart in front of a person. A kind
    waits when it stands behind access to the estate (``requires_approval``)
    or the public internet sees it (``public_effect``).
    """

    if provider.requires_approval:
        return True
    # Adopting makes HQ's copy equal what is live, so nothing public moves.
    return provider.public_effect and (spec is None or spec.name not in _copies_live())


def _waits(provider, principal: Principal, spec=None) -> bool:
    """The one rule under the hold and the write floor.

    A kind that stands behind access waits for anybody without a person
    behind them. One the public sees waits for an agent, as its delete does,
    so an operator's own change, from the host's shell or their device, runs.
    """

    if not needs_approval(provider, spec):
        return False
    return provider.requires_approval or is_agent(principal.interface)


def _copies_live() -> frozenset[str]:
    return frozenset({ADOPT, ADOPT_SERVICE, ADOPT_ZONE})


def consent_gap(kind: str, *, principal: Principal) -> Sentence:
    """Why this act has no consent behind it, or "" when it has.

    The hold's rule, asked again from inside the use cases that write, since
    the hold sees only capabilities that declare they act on an infrastructure
    resource and one that writes a declaration without saying so would pass it.
    A string, so the caller raises its own domain error and this module imports
    none of the use cases that call it.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or not _waits(provider, principal):
        return ""
    if is_interactive(principal) or principal.approved_by:
        return ""
    return "This change needs approval. Ask for it through the API or MCP, then approve it in HQ in the browser."
