"""Controller calls the bridge refused, kept until the same call is accepted.

A refused controller looks like a quiet one, its readings only aging, so this
is the record that its reports stopped landing, and why. Written only by the
bridge from a refusal it already answered; a row holds the refusal's detail
struck of secrets, never the payload, and is replaced in place.
"""

import logging

from django.db import DatabaseError
from django.utils import timezone

from hq.domains.control_plane.models import RefusedReport
from hq.domains.control_plane.secret_shapes import struck

logger = logging.getLogger("severino.bridge")

# The action the controller's readings arrive by (``bridge_actions.ACTIONS``).
READINGS_ACTION = "inventory"

_DETAIL = RefusedReport._meta.get_field("detail").max_length
_CONTROLLER = RefusedReport._meta.get_field("controller_id").max_length


def note(action: str, controller_id: str, status: int, detail: str) -> None:
    """Keep that the bridge refused ``action`` for ``controller_id``, replacing the last."""

    said = struck(detail)[0]
    try:
        RefusedReport.objects.update_or_create(
            action=action,
            controller_id=controller_id[:_CONTROLLER],
            defaults={"status": status, "detail": said[:_DETAIL], "refused_at": timezone.now()},
        )
    except DatabaseError:
        logger.exception("bridge.refusal_unkept action=%s", action)


def accepted(action: str, controller_id: str) -> None:
    """Close what was refused of ``action`` for this controller, or for a
    controller the refused call did not name.

    Read before it is deleted: a delete is a write, and every call the bridge
    serves passes here.
    """

    rows = RefusedReport.objects.filter(action=action, controller_id__in={controller_id[:_CONTROLLER], ""})
    try:
        if rows.exists():
            rows.delete()
    except DatabaseError:
        logger.exception("bridge.refusal_unclosed action=%s", action)


def open_refusals() -> tuple[RefusedReport, ...]:
    """Every refusal no accepted call has closed."""

    return tuple(RefusedReport.objects.all())
