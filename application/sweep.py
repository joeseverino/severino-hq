"""Recording what the controller found, and taking on what it may.

Two steps that must happen together and belong to different owners.
`inventory` knows how to store a sweep; `zones` knows which of the records in
it fall inside a domain HQ has been made responsible for. Neither is the right
place to know about the other -- when `inventory` reached up into `zones` for
the adoption step the two imported each other, which is the kind of knot that
tightens every time something is added to either side.

So the composition lives above both, where it can see them and they cannot see
it. The transaction is held here, because the guarantee is about the pair:
adoption reads specs back out of the records the sweep just wrote, so a failure
partway through must take the stored sweep with it rather than leave
declarations describing a world nothing recorded.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from .inventory import (
    adopt_discovered,
    adopt_discovered_containers,
    record_inventory,
)
from .zones import adopt_discovered_records


@transaction.atomic
def record_sweep(
    payload: dict[str, Any], *, principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store a controller sweep, then adopt what it revealed."""

    result = record_inventory(
        payload, principal=principal, controller_id=controller_id
    )
    # Deliberately after the sweep is stored: adoption reads each spec back out
    # of the records just recorded, so every declaration it writes starts equal
    # to what the controller actually found and the first reconcile is a no-op.
    adopted = adopt_discovered_records(principal=principal)["adopted"]
    # Containers too, for the same reason and by the same rule: the decision
    # was made when the credential was added, not once per container.
    adopted += adopt_discovered_containers(principal=principal)["adopted"]
    # The policy too, so it is a declaration an operator can open and change
    # rather than something only Tailscale's own console can edit.
    adopted += adopt_discovered("tailscale.policy", principal=principal)["adopted"]
    return {**result, "adopted": adopted}
