"""Each unhandled error HQ logs while serving a request, kept where its request id reads it.

A handler on the root logger, as Django's ``AdminEmailHandler`` is on
``django.request``: an error record with a traceback, logged inside a request,
becomes one ``ServerError`` row keyed by that request's id. The traceback is
the text the log line carries (``LineFormatter``, which tells a validation
failure without the value sent), never a frame's locals. Writing it never
raises into the request and never changes its answer: a failure to record is
logged without a traceback, which this handler does not take, and dropped. A
speculative request records nothing, since the browser discards its error and
asks again.
"""

import logging
from typing import override

from django.db import transaction
from django.utils.text import Truncator

lazy from hq.platform.application import request_context
lazy from hq.platform.application.request_identity import signed_in

from . import log_lines
from .tracing import NO_TRACE
lazy from . import speculation
lazy from .attribution import bound
lazy from .models import ServerError

logger = logging.getLogger("severino.server_errors")

# The newest this many are kept; each write deletes what falls past them.
KEPT = 200
# A traceback past this many characters keeps its end, where the error is.
TRACEBACK_CHARS = 64 * 1024
MESSAGE_CHARS = 2000

_formatter = log_lines.LineFormatter()


class ServerErrorHandler(logging.Handler):
    """Keeps each error record with a traceback that a request logged, the first of each request."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if not record.exc_info or record.exc_info[1] is None:
            return
        try:
            keep(record, record.exc_info[1])
        except Exception as exc:  # noqa: BLE001 - recording an error never fails the request it served
            logger.warning(
                "A server error was not recorded: %s",
                exc.__class__.__name__,
                extra={"event": "hq.server_error.unrecorded"},
            )


def keep(record: logging.LogRecord, exc: BaseException) -> None:
    """Write ``exc`` as its request's error, unless it was served outside a request or on a guess."""

    request = getattr(record, "request", None) or request_context.current()
    request_id = log_lines.request_id(record)
    if request_id == NO_TRACE or (request is not None and speculation.purpose(request)):
        return
    door = bound()
    user = signed_in(request) if request is not None else None
    if user is None and door is not None and door.person is not None:
        user = door.person()
    traceback = _formatter.formatException((type(exc), exc, exc.__traceback__))
    if len(traceback) > TRACEBACK_CHARS:
        traceback = "…" + traceback[-TRACEBACK_CHARS:]
    field = ServerError._meta.get_field
    # A request's first error is its own; a later one it logs is the same failure retold.
    with transaction.atomic():
        ServerError.objects.get_or_create(
            request_id=request_id,
            defaults={
                "method": str(getattr(request, "method", "") or "")[: field("method").max_length],
                "path": Truncator(str(getattr(request, "path", "") or "")).chars(field("path").max_length),
                "user": user if user is not None and user.is_authenticated else None,
                "actor": door.actor if door is not None else "",
                "interface": door.interface if door is not None else "",
                "exception": _named(type(exc))[-field("exception").max_length :],
                "message": Truncator(str(log_lines.logged_failure(exc))).chars(MESSAGE_CHARS),
                "traceback": traceback,
            },
        )
        prune()


def _named(kind: type[BaseException]) -> str:
    return kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"


def prune() -> int:
    """Delete every error past the newest ``KEPT``; return how many."""

    newest = ServerError.objects.order_by("-pk").values_list("pk", flat=True)
    oldest_kept = next(iter(newest[KEPT - 1 : KEPT]), None)
    if oldest_kept is None:
        return 0
    return ServerError.objects.filter(pk__lt=oldest_kept).delete()[0]
