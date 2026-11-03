"""Holding a secret for one resource, sealed so only the controller can read it.

``set_secret`` is the one writer of ``SealedSecret``. The web seals and forgets:
nothing here reads a value back, and the audit record says which slot changed
and to which version, never anything derived from the value.
"""

from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, SealedSecret
from hq.platform.core import secrets
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog

from .security import Principal, person_of

AUDIT_LABEL = "Sealed secret"


@transaction.atomic
def set_secret(resource: ManagedResource, slot: str, plaintext: str, principal: Principal) -> SealedSecret:
    """Seal ``plaintext`` into ``slot`` on ``resource``, replacing what it held.

    Raises ``secrets.SecretsUnavailable`` before writing anything when HQ has no
    key to seal to.
    """

    sealed = secrets.seal_for_controller(plaintext, resource_key=resource.key, slot=slot)
    row = SealedSecret.objects.select_for_update().filter(resource=resource, slot=slot).first()
    now = timezone.now()
    if row is None:
        row = SealedSecret.objects.create(
            resource=resource, slot=slot, sealed=sealed, version=1, set_at=now, set_by=principal.actor
        )
        change = "set"
    else:
        row.sealed = sealed
        row.version += 1
        row.set_at = now
        row.set_by = principal.actor
        row.save(update_fields=("sealed", "version", "set_at", "set_by"))
        change = "replaced"
    record_event(
        action=AuditLog.Action.CREATED if change == "set" else AuditLog.Action.UPDATED,
        obj=row,
        type_label=AUDIT_LABEL,
        message=f"{slot} {change} on {resource.key} (version {row.version})",
        metadata={"resource": resource.key, "slot": slot, "change": change, "version": row.version},
        user=person_of(principal),
        required=True,
    )
    return row


def sealed_slots(resource_key: str) -> dict[str, str]:
    """Each slot a resource holds, sealed, for the controller to open."""

    return dict(SealedSecret.objects.filter(resource__key=resource_key).values_list("slot", "sealed"))
