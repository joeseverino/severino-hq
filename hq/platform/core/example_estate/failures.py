"""Two requests HQ failed, kept as a running HQ keeps them (``server_errors``), so Errors and an error's page draw."""

import linecache
import logging
from datetime import timedelta

from django.test import RequestFactory

from hq.platform.core import server_errors
from hq.platform.core.attribution import outside_every_door
lazy from hq.platform.core.models import ServerError

from .names import ZONE, back, hexed

_VIEWS = "/srv/example/views.py"
# Each failure: the request, how long ago, and the view that raised, compiled
# under a file of its own so its traceback names no path of the machine building it.
FAILURES = (
    (
        "get",
        "/infrastructure/topology/",
        timedelta(hours=2),
        "def view():\n    return nodes()['example-host']\n\ndef nodes():\n    return {}\n",
    ),
    (
        "post",
        f"/domains/{ZONE}/mail/",
        timedelta(days=1),
        "def view():\n    raise RuntimeError('The example provider answered 502.')\n",
    ),
)


def _raised(source: str) -> Exception:
    names: dict[str, object] = {}
    # Its lines held as a file's, so the traceback quotes each frame's line.
    linecache.cache[_VIEWS] = (len(source), None, source.splitlines(keepends=True), _VIEWS)
    exec(compile(source, _VIEWS, "exec"), names)  # noqa: S102 - the estate's own fixed source
    try:
        names["view"]()  # type: ignore[operator]
    except Exception as raised:  # noqa: BLE001 - every example failure is kept alike
        # From the view down: the frame that called it is this module's.
        return raised.with_traceback(raised.__traceback__.tb_next if raised.__traceback__ else None)
    raise AssertionError("An example view returned.")


def raised(owner) -> None:
    for method, path, age, source in FAILURES:
        request = getattr(RequestFactory(), method)(path)
        request.user = owner
        request.request_id = hexed("server error", path, length=32)
        with outside_every_door():
            server_errors.keep(logging.makeLogRecord({"request": request}), _raised(source))
        ServerError.objects.filter(request_id=request.request_id).update(occurred_at=back(age, path))
