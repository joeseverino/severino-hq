"""
Middleware for Severino HQ.

- LoginRequiredMiddleware: this is a single-user / internal app; every view
  requires a session unless it is marked ``@login_not_required``.
- CurrentUserMiddleware: scopes the request user to the active ASGI context so
  ORM signals can attribute audit events without leaking across requests.
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
from time import monotonic
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import middleware as auth_middleware

from hq.platform.application.arrivals import note as note_arrival
from hq.platform.application.cadence import note_activity
from hq.platform.application.demo import demo_scope

import hq.platform.core.logging as request_logging
from hq.platform.core.outbound import serving
from hq.platform.application import request_context


# Where the browser's own answer to "show me stand-ins" is kept. Named here
# because the middleware that reads it and the view that writes it are the only
# two things that may know the spelling.
DEMO_SESSION_KEY = "showing_demo"

_current_user = ContextVar("severino_current_user", default=None)
_request_logger = logging.getLogger("severino.request")


class DemoModeMiddleware:
    """Enter the substituting scope for a request whose session asked for it.

    Middleware rather than a context processor, because the substitution has to
    be in force while the view runs and not merely while the page renders,
    every number on a page is decided long before a template sees it.

    Session-scoped on purpose. The flag never leaves the browser that set it, so
    it cannot follow an operator to a second device, cannot reach the API or the
    MCP, and cannot be left switched on for somebody else. Anonymous requests
    never carry one: the sign-in page has nothing to substitute, and a flag that
    survives sign-out belongs to nobody.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        showing = bool(
            getattr(request, "user", None)
            and request.user.is_authenticated
            and request.session.get(DEMO_SESSION_KEY)
        )
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


class RequestContextMiddleware:
    """Attach a server-generated correlation ID, the time taken, and one bounded access log."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid4().hex
        request.request_id = request_id
        # How often the controller sweeps depends on whether anybody is here.
        # A stat on most requests and a small write on the first of each
        # interval; see `application.cadence`. A probe is not anybody: counting
        # it would hold the active cadence open for as long as the container is
        # healthy.
        if not _is_health_probe(request):
            note_activity()
            note_arrival(request)
        token = request_logging.set_request_id(request_id)
        bound = request_context.bind(request)
        started = monotonic()
        try:
            # Everything a request does is held to answering from what HQ
            # holds: see `core.outbound`.
            with serving(request):
                response = self.get_response(request)
            response["X-Request-ID"] = request_id
            # Django has settings for the other browser-boundary headers but
            # not these three. HQ uses none of these APIs, and an operator
            # console holding provider credentials has no reason to leave them
            # available to anything that manages to run in the page.
            response.setdefault(
                "Permissions-Policy",
                "geolocation=(), microphone=(), camera=(), usb=(), payment=(), "
                "interest-cohort=()",
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
        finally:
            request_context.unbind(bound)
            request_logging.reset_request_id(token)


def get_current_user():
    return _current_user.get()


def set_current_user(user) -> None:
    _current_user.set(user)


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None:
            # AuthenticationMiddleware exposes a SimpleLazyObject. Keeping it
            # in a ContextVar lets ASGI's context restoration evaluate the
            # session-backed object on the event-loop thread (Django 6.1 then
            # correctly raises SynchronousOnlyOperation). Resolve it while
            # this synchronous middleware is still running in its worker.
            user.is_authenticated
            user = getattr(request, "_cached_user", user)
        token = _current_user.set(user)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


class LoginRequiredMiddleware(auth_middleware.LoginRequiredMiddleware):
    """Django's sign-in gate: every view needs a session unless it is marked
    ``@login_not_required`` where it is defined.

    Also lets through the routes an extension declared as carrying their own
    authentication. Skipping the redirect is not skipping auth: the view
    still authenticates the request, and answers 401 rather than an HTML
    login page a native client cannot use.
    """

    def process_view(self, request, view_func, view_args, view_kwargs):
        from hq.platform.application.plugins import plugin_token_authenticated_prefixes

        if request.path.startswith(plugin_token_authenticated_prefixes()):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)


class ProjectionMiddleware:
    """One read projection per page request, seeded with where it arrived.

    Every composer on a GET shares one machine catalogue, one relation graph and
    one set of readings, and knows the address and port HQ was reached on. A
    write is left unscoped so it never reads a value it has just changed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ("GET", "HEAD"):
            return self.get_response(request)
        from hq.platform.application.hq_self import serving
        from hq.platform.application.projection import projection_scope

        with projection_scope(seed=serving(request)):
            response = self.get_response(request)
            # A template response renders after the view returns; inside the scope.
            if hasattr(response, "render") and not getattr(response, "is_rendered", True):
                response.render()
            return response
