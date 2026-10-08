"""What the estate's derived facts read, declared once.

Every model a derivation queries is named here, so the topology, the findings
and the action queue agree on what a change to the estate is. A host table a
derivation reads without declaring fails ``test_derivations``. An installed
extension's tables cannot be named here: a derivation learns those the first
time it reads them and keys its answers on them from then on (``derivations``).
"""

from typing import Any

# The estate: what is declared, what the controller observed, and the readings
# and registry records joined to them.
ESTATE_READS: tuple[str, ...] = (
    "control_plane.ManagedResource",
    "control_plane.ProviderInventory",
    "control_plane.ProviderConnection",
    "control_plane.NotManaged",
    "control_plane.OperationRequest",
    "control_plane.ApprovalRequest",
    "control_plane.ReadRequest",
    "control_plane.CertificateMaterial",
    "control_plane.AddressReading",
    "control_plane.CapabilityRule",
    "core.UpstreamReading",
    "projects.Project",
    "analytics.AnalyticsSite",
    "analytics.RumDaily",
)

# The composed queue: the estate, and each record domain that reports work.
QUEUE_READS: tuple[str, ...] = ESTATE_READS + (
    "assets.Asset",
    "content.ContentItem",
    "content.ContentItem_related_documentation",
    "docs_index.DocumentationRecord",
    "expenses.Expense",
    "receipts.Receipt",
)


# The dashboard's cards and overviews: the estate, and each record domain's
# headline reading.
DASHBOARD_READS: tuple[str, ...] = ESTATE_READS + (
    "content.ContentItem",
    "docs_index.DocumentationRecord",
    "expenses.Expense",
)



def composed_variant() -> tuple[Any, ...]:
    """What a composition of every domain depends on besides rows and the clock.

    What the estate does, whether the reader asked for a demo, which an
    extension's part is worded by, and which providers compose it.
    """

    from .demo import showing_demo
    from .domains import composition

    return (*estate_variant(), showing_demo(), composition())


def estate_variant(principal: Any = None) -> tuple[Any, ...]:
    """What an estate derivation depends on besides rows and the clock.

    Who is reading, since a projection is narrowed to what they may see; the
    address and port the request reached HQ on, which place HQ's own service;
    and whether HQ is in use, which sets the sweep interval a finding quotes.
    """

    from .cadence import recently_used
    from .hq_self import scoped_served_at, scoped_served_port

    reader: tuple[Any, ...] = ()
    if principal is not None:
        reader = (
            principal.actor,
            principal.interface,
            tuple(sorted(str(item) for item in principal.capabilities)),
        )
    return (*reader, tuple(scoped_served_at()), scoped_served_port(), recently_used())
