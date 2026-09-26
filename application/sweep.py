"""Recording what the controller found, and taking on what it may.

Two steps that must happen together and belong to different owners.
`inventory` knows how to store a sweep; `zones` knows which of the records in
it fall inside a domain HQ has been made responsible for. Neither is the right
place to know about the other, when `inventory` reached up into `zones` for
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
    confirm_observed,
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
    # After the sweep is stored: adoption reads each spec back out of the
    # records just recorded, so every declaration it writes starts equal to
    # what the controller found and the first reconcile is a no-op.
    #
    # Only what a connection that manages reached. A connection that only
    # observes adopts nothing, and a record an operator stopped managing stays
    # out. See ``application.adoption``.
    adopted: list[str] = []
    for kind in _adoptable_kinds():
        adopted += adopt_discovered(kind, principal=principal)["adopted"]
    # Records last, and separately, because they are the one kind whose
    # adoption is scoped by something other than the credential: a record
    # belongs to a domain, so `zones` takes the ones inside domains HQ holds.
    # Run after the loop above so a zone adopted moments ago already counts,
    # otherwise its records would wait a whole sweep for no reason.
    adopted += adopt_discovered_records(principal=principal)["adopted"]
    # And everything already declared that the sweep just found unchanged, so
    # a declaration nothing touched still reads as observed.
    confirmed = confirm_observed(payload)
    return {**result, "adopted": adopted, "confirmed": confirmed}


def _adoptable_kinds() -> tuple[str, ...]:
    """Every kind currently sitting unadopted, in a stable order.

    Read from what the sweep just recorded rather than named here, so this
    cannot fall behind the provider registry.
    """

    from .inventory import unmanaged

    from .zones import RECORD_KIND

    # Everything except records, which `zones` adopts by domain rather than one
    # at a time. Domains are in, through a connection that manages.
    return tuple(sorted({item.kind for item in unmanaged()} - {RECORD_KIND}))
