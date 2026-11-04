"""Controller calls the bridge refused, kept until the same call is accepted.

A refusal runs nothing of the action, so a controller whose reports HQ will
not take looks like a quiet one: its readings only age. This is the record
that they stopped landing, and why, which ``report-refused`` says and a rule
over an aged reading names as its cause.

Security: written only by the bridge application, from a refusal it already
answered (docs/Architecture.md#the-controller). A row holds the action's name,
the controller its query named once the query was held to the contract, and
the refusal's detail, which never repeats a value: struck again of secrets
here and cut to the problem's length. Never the payload. One row per action and
controller, replaced in place, so a controller refused every pass writes no
history. Best effort: the controller is answered whether or not this is kept.
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
