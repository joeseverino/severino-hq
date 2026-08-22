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
    #
    # Records inside a domain HQ was made responsible for are `zones`' to
    # decide, because the responsibility is the domain rather than the record.
    adopted = adopt_discovered_records(principal=principal)["adopted"]
    # Everything else a credential reached, whatever kind it is. If HQ can see
    # it, HQ manages it: the decision was made when the credential was added,
    # and asking again per record is a question whose answer is always yes.
    # Listing the kinds here meant a provider added later stayed unmanaged
    # until somebody remembered to add it to this line -- and in the meantime
    # its records sat in a list of things to opt into one at a time.
    for kind in _adoptable_kinds():
        adopted += adopt_discovered(kind, principal=principal)["adopted"]
    return {**result, "adopted": adopted}


def _adoptable_kinds() -> tuple[str, ...]:
    """Every kind currently sitting unadopted, in a stable order.

    Read from what the sweep just recorded rather than named here, so this
    cannot fall behind the provider registry.
    """

    from .inventory import unmanaged

    from .zones import RECORD_KIND, ZONE_KIND

    # Everything except the two that a domain governs rather than a credential.
    #
    # Taking on a domain is a decision -- it says HQ is responsible for what
    # that name resolves to -- and a sweep that made it would claim every zone
    # the token can see. The records inside one follow from that decision, so
    # `zones` adopts those, for domains HQ was actually made responsible for.
    # Everything else is a thing a credential plainly already manages, where
    # asking per record is a question whose answer is always yes.
    return tuple(
        sorted({item.kind for item in unmanaged()} - {RECORD_KIND, ZONE_KIND})
    )
