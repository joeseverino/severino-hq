"""The default hold: which changes wait for a person before any operator rule.

One rule under the capability runner's hold and the consent every
declaration write asks (``capabilities.Consented.admits``), so the two cannot
disagree, and read by the provider contract and the policy page to say it.
"""

from typing import Any

from hq.domains.control_plane.models import ManagedResource
lazy from hq.domains.control_plane.providers import PROVIDERS

from .approvals import DECLARATIONS
from .security import Principal, is_agent
lazy from .adoption import ADOPT, ADOPT_SERVICE
lazy from .zones import ADOPT_ZONE


def may_be_held_by_default(spec) -> bool:
    """The capability-level half of held_by_default, for describing a default without a target."""

    return spec.effect.changes and spec.subject_resource == DECLARATIONS


def held_by_default(spec, command: Any, target: Any, *, principal: Principal) -> bool:
    """The default before any operator rule: a change to a gated declaration waits."""

    if not may_be_held_by_default(spec):
        return False
    if target is not None:
        kind = ManagedResource.objects.filter(key=str(target)).values_list("kind", flat=True).first()
    else:
        kind = str(getattr(command, "kind", ""))
    provider = PROVIDERS.get(kind) if kind else None
    return provider is not None and kind_waits(provider, principal, spec)


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


def kind_waits(provider, principal: Principal, spec=None) -> bool:
    """The one rule under the hold and the consent a declaration write asks.

    A kind that stands behind access waits for anybody without a person
    behind them. One the public sees waits for an agent, as its delete does,
    so an operator's own change, from the host's shell or their device, runs.
    """

    if not needs_approval(provider, spec):
        return False
    return provider.requires_approval or is_agent(principal.interface)


def _copies_live() -> frozenset[str]:
    return frozenset({ADOPT, ADOPT_SERVICE, ADOPT_ZONE})
