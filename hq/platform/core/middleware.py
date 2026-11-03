"""
Middleware for Severino HQ.

- LoginRequiredMiddleware: every view requires a session unless it is marked
  ``@login_not_required``, and a page the capability it reads under.
"""

import logging
from contextlib import AbstractContextManager
from time import monotonic
from typing import override
from uuid import uuid7

from django.conf import settings
from django.contrib.auth import middleware as auth_middleware
from django.core.exceptions import PermissionDenied
from django.template.response import SimpleTemplateResponse
from django.urls import Resolver404, resolve

import hq.platform.core.logging as request_logging
from hq.platform.application import request_context
from hq.platform.application.arrivals import ARRIVALS, note as note_arrival
from hq.platform.application.cadence import note_activity
from hq.platform.application.demo import demo_scope
from hq.platform.core import speculation
from hq.platform.core.attribution import operation_context
from hq.platform.core.outbound import serving
from hq.platform.core.write_bounds import NOTHING, Bound, writes
lazy from hq.platform.api.urls import app_name as API_APP
lazy from hq.platform.application import hq_self
lazy from hq.platform.application.pages import authorizes_itself, page_refusal
lazy from hq.platform.application.projection import projection_scope
lazy from hq.platform.application.request_identity import signed_in
lazy from hq.platform.application.security import API_INTERFACE, WEB_INTERFACE

# Where the browser's own answer to "show me stand-ins" is kept. Named here
# because the middleware that reads it and the view that writes it are the only
# two things that may know the spelling.
DEMO_SESSION_KEY = "showing_demo"

_request_logger = logging.getLogger("severino.request")


@writes(NOTHING)
class DemoModeMiddleware:
    """Enter the substituting scope for a request whose session asked for it.

    Middleware, so the substitution is in force while the view decides every
    number, not only while the page renders. Session-scoped, so the flag never
    leaves the browser that set it: it cannot follow an operator to a second
    device, reach the API or the MCP, or be left switched on for somebody else.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # An anonymous request never carries the flag: the sign-in page has
        # nothing to substitute, and a flag that survives sign-out belongs to nobody.
        showing = bool(signed_in(request) is not None and request.session.get(DEMO_SESSION_KEY))
        request.showing_demo = showing
        with demo_scope(showing):
            return self.get_response(request)


def _is_health_probe(request) -> bool:
    """Whether this request is a probe rather than a caller.

    One definition, used by both the activity marker and the access log, so the
    two cannot disagree about what counts as traffic.
    """

    return request.path.startswith("/health/")


# A request slower than this is logged as a warning, so it is found without
# reading every line. Pages answer in a tenth of it (`manage.py bench_pages`).
SLOW_REQUEST_MS = 1000


# The operation of a request no route answers.
UNROUTED = "unrouted"


def request_operation(path: str, *, operation_id: str) -> AbstractContextManager[None]:
    """A request for ``path`` as one operation: through the machine API or the
    web, named by the route it resolves to, under ``operation_id``, by whoever
    it has signed in when each row is written."""

    try:
        route = resolve(path)
    except Resolver404:
        route = None
    return operation_context(
        interface=API_INTERFACE if route is not None and API_APP in route.app_names else WEB_INTERFACE,
        actor="",
        operation=(route.view_name or route.route) if route is not None else UNROUTED,
        operation_id=operation_id,
    )


# One file's modification time (``cadence.note_activity``), touched at most once
# every SEVERINO_ACTIVITY_THROTTLE_SECONDS.
ACTIVITY = Bound("activity marker", "one file, touched at most once an interval")


@writes(ACTIVITY, ARRIVALS)
class RequestContextMiddleware:
    """Attach a server-generated correlation ID, the operation every audit row
    of the request belongs to, the time taken, and one bounded access log."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid7().hex
        request.request_id = request_id
        # How often the controller sweeps depends on whether anybody is here.
        # A stat on most requests and a small write on the first of each
        # interval; see `application.cadence`. A probe is not anybody: counting
        # it would hold the active cadence open for as long as the container is
        # healthy.
        if not _is_health_probe(request):
            note_activity()
            note_arrival(request)
        started = monotonic()
        with (
            request_logging.set_request_id(request_id),
            request_context.bind(request),
            request_operation(request.path_info, operation_id=request_id),
        ):
            # Everything a request does is held to answering from what HQ
            # holds: see `core.outbound`.
            # A page fetched on a guess is further held to having no effect:
            # see `core.speculation`.
            with serving(request):
                response = speculation.answer(request, self.get_response)
            response["X-Request-ID"] = request_id
            # Django has settings for the other browser-boundary headers but
            # not these three. HQ uses none of these APIs, and an operator
            # console holding provider credentials has no reason to leave them
            # available to anything that manages to run in the page.
            response.setdefault(
                "Permissions-Policy",
                "geolocation=(), microphone=(), camera=(), usb=(), payment=(), interest-cohort=()",
            )
            # Nothing here is meant to be read by another origin. Django's
            # default opener policy already isolates the browsing context;
            # this is the other half: another site cannot pull a page, an
            # export or a receipt into its own document as a subresource, so a
            # cross-origin read cannot be laundered through an <img> or a
            # <script> tag and measured.
            response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
            # Where `report-to` in the policy resolves to. Named `csp` because
            # that is the group the policy references; the endpoint is on this
            # origin, so a report never leaves the tailnet.
            response.setdefault(
                "Reporting-Endpoints",
                f'csp="{settings.SEVERINO_CSP_REPORT_PATH}"',
            )
            duration_ms = round((monotonic() - started) * 1000, 2)
            # The application's own time, where the operator is already
            # looking: the browser's network panel shows it beside the request.
            response["Server-Timing"] = f"app;dur={duration_ms}"
            slow = duration_ms >= SLOW_REQUEST_MS
            if slow or not _is_health_probe(request) or response.status_code >= 500:
                _request_logger.log(
                    logging.WARNING if slow else logging.INFO,
                    "slow request" if slow else "request completed",
                    extra={
                        "event": "http.request",
                        "method": request.method,
                        "path": request.path,
                        "status": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
            return response

    def process_view(self, _request, view_func, _view_args, _view_kwargs):
        return speculation.before_view(view_func)

    def process_exception(self, _request, exception):
        return speculation.on_exception(exception)


@writes(NOTHING)
class LoginRequiredMiddleware(auth_middleware.LoginRequiredMiddleware):
    """Django's sign-in gate: every view needs a session unless it is marked
    ``@login_not_required`` where it is defined.

    Also lets through the routes an extension declared as carrying their own
    authentication. Skipping the redirect is not skipping auth: the view
    still authenticates the request, and answers 401 rather than an HTML
    login page a native client cannot use.

    A signed-in person then opens a page only while their role holds the
    capability it reads under (``pages.page_refusal``). Here, before the view,
    because this is the one place every routed view passes through.
    """

    @override
    def process_view(self, request, view_func, view_args, view_kwargs):
        if authorizes_itself(view_func):
            return None
        redirected = super().process_view(request, view_func, view_args, view_kwargs)
        if redirected is not None:
            return redirected
        refusal = page_refusal(request.user, view_func)
        if refusal is not None:
            # Django's own 403 page, which names no capability and shows nothing of the page's.
            raise PermissionDenied(refusal.message)
        return None


@writes(NOTHING)
class ProjectionMiddleware:
    """One read projection per page request, seeded with where it arrived.

    Every composer on a GET shares one machine catalog, one relation graph and
    one set of readings, and knows the address and port HQ was reached on. A
    write is left unscoped so it never reads a value it has just changed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ("GET", "HEAD"):
            return self.get_response(request)
        with projection_scope(seed=hq_self.serving(request)):
            response = self.get_response(request)
            # A template response renders after the view returns; inside the scope.
            if isinstance(response, SimpleTemplateResponse) and not response.is_rendered:
                response.render()
            return response
